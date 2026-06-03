from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── 配置常量 ────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT_RANGE_START = 8200
PORT_RANGE_END = 8399  # 含
READY_TIMEOUT = 15.0  # 启动后等待端口可连接的最长秒数
READY_POLL_INTERVAL = 0.3
SWEEP_INTERVAL = 60.0  # sweeper 循环间隔秒
IDLE_TIMEOUT = 1800.0  # 闲置回收（无心跳）秒
MAX_LIFETIME = 21600.0  # 最大寿命秒
LOG_TAIL_LINES = 200  # 内存中保留的日志尾部行数

# 部署工作目录根：agent_flow/temp/deployments/<id>/
_THIS_FILE = Path(__file__).resolve()
# infra/deploy/manager.py -> 上溯三级到 agent_flow
_AGENT_FLOW_ROOT = _THIS_FILE.parents[2]
DEPLOY_ROOT = _AGENT_FLOW_ROOT / "temp" / "deployments"


@dataclass
class Deployment:
    deployment_id: str
    kind: str
    title: str
    command: str
    workdir: str
    port: int
    url: str
    status: str  # "starting" | "running" | "failed" | "stopped"
    pid: Optional[int] = None
    created_at: float = 0.0
    last_seen: float = 0.0
    error: str = ""
    logs: list[str] = field(default_factory=list)
    # 运行期句柄，不参与对外序列化
    _process: Any = field(default=None, repr=False, compare=False)
    _log_task: Any = field(default=None, repr=False, compare=False)

    def to_public_dict(self) -> dict[str, Any]:
        """供 list()/接口返回的纯 dict（可被 FastAPI JSON 序列化）。"""
        return {
            "deployment_id": self.deployment_id,
            "title": self.title,
            "url": self.url,
            "port": self.port,
            "status": self.status,
            "kind": self.kind,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
        }


def _pick_free_port() -> int:
    """在 8200-8399 内用 socket 绑定 127.0.0.1 测试取一个空闲端口。"""
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HOST, port))
        except OSError:
            continue
        finally:
            sock.close()
        return port
    raise RuntimeError(
        f"端口池 {PORT_RANGE_START}-{PORT_RANGE_END} 已无空闲端口"
    )


def _tcp_can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class DeploymentManager:
    """进程内单例：拉起/回收后台部署进程。"""

    def __init__(self) -> None:
        self._deployments: dict[str, Deployment] = {}
        self._sweeper_task: Optional[asyncio.Task] = None

    # ── 公共接口 ────────────────────────────────────────────────────
    async def create(
        self,
        *,
        kind: str,
        title: str,
        files: dict[str, str] | None,
        command: str | None,
        entry: str | None,
        env: dict | None,
        agent_id: str,
        run_id: str,
        source_dir: str | None = None,
        base_dir: str | None = None,
    ) -> Deployment:
        """拉起一个后台部署。

        两种工作目录来源：
          - 不传 source_dir：在 DEPLOY_ROOT/<id> 新建工作目录，把 files 落地进去（native deploy 工具用）。
          - 传 source_dir：直接托管/运行一个**已存在的目录**（coding agent 在自己 workdir 里建好的产物），
            source_dir 相对 base_dir 解析并做越界防护；仍允许 files 往该目录补写启动文件。
        """
        kind = (kind or "").strip().lower()
        if kind not in ("static", "command"):
            raise ValueError(f"不支持的部署类型 kind={kind!r}（仅 static / command）")

        deployment_id = uuid.uuid4().hex
        if source_dir:
            workdir = self._resolve_source_dir(source_dir, base_dir)
        else:
            workdir = DEPLOY_ROOT / deployment_id
            workdir.mkdir(parents=True, exist_ok=True)

        # 落地文件（source_dir 模式下也允许补写启动文件；路径越界已在 _write_files 防护）
        self._write_files(workdir, files or {})

        entry = (entry or "index.html").strip() or "index.html"
        port = _pick_free_port()
        now = time.time()

        deployment = Deployment(
            deployment_id=deployment_id,
            kind=kind,
            title=title or deployment_id,
            command=command or "",
            workdir=str(workdir),
            port=port,
            url=f"http://{HOST}:{port}",
            status="starting",
            pid=None,
            created_at=now,
            last_seen=now,  # 初始宽限期
            error="",
            logs=[],
        )
        self._deployments[deployment_id] = deployment

        try:
            if kind == "static":
                await self._start_static(deployment, entry)
            else:
                await self._start_command(deployment, command, env)
        except Exception as exc:  # 启动失败
            deployment.status = "failed"
            deployment.error = (deployment.error or str(exc))[-4000:]
            await self._kill_process(deployment)
            self._ensure_sweeper()
            return deployment

        # 异步轮询就绪
        ready = await self._wait_ready(deployment)
        if ready:
            deployment.status = "running"
            deployment.last_seen = time.time()
        else:
            deployment.status = "failed"
            tail = "\n".join(deployment.logs[-30:])
            deployment.error = (
                f"启动后 {READY_TIMEOUT:.0f}s 内端口 {deployment.port} 未就绪。"
                + (f" 日志尾部:\n{tail}" if tail else "")
            )[-4000:]
            await self._kill_process(deployment)

        self._ensure_sweeper()
        return deployment

    async def stop(self, deployment_id: str) -> bool:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            return False
        ok = await self._kill_process(deployment)
        deployment.status = "stopped"
        return ok

    def touch(self, deployment_id: str) -> bool:
        """刷新 last_seen，供闲置回收。"""
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            return False
        deployment.last_seen = time.time()
        return True

    def get(self, deployment_id: str) -> Optional[Deployment]:
        return self._deployments.get(deployment_id)

    def list(self) -> list[dict]:
        return [d.to_public_dict() for d in self._deployments.values()]

    # ── 内部：source_dir 解析（托管已存在目录）──────────────────────
    def _resolve_source_dir(self, source_dir: str, base_dir: str | None) -> Path:
        if not base_dir:
            raise ValueError("source_dir 模式必须提供 base_dir（部署目录的根，通常是 agent 的 workdir）")
        base = Path(base_dir).resolve()
        candidate = Path(source_dir)
        target = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(f"source_dir 越界（必须位于 {base} 内）: {source_dir}")
        if not target.is_dir():
            raise ValueError(f"source_dir 不存在或不是目录: {source_dir}")
        return target

    # ── 内部：文件落地 ──────────────────────────────────────────────
    def _write_files(self, workdir: Path, files: dict[str, str]) -> None:
        root = workdir.resolve()
        for rel_path, content in (files or {}).items():
            if not isinstance(rel_path, str) or not rel_path.strip():
                continue
            # 防止路径穿越
            target = (root / rel_path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise ValueError(f"非法文件路径（越界）: {rel_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                content if isinstance(content, str) else str(content),
                encoding="utf-8",
            )

    # ── 内部：启动 ──────────────────────────────────────────────────
    async def _start_static(self, deployment: Deployment, entry: str) -> None:
        workdir = deployment.workdir
        # entry 仅作记录/校验
        entry_path = Path(workdir) / entry
        if not entry_path.exists():
            self._append_log(
                deployment,
                f"[warn] 入口文件不存在: {entry}（http.server 仍会托管目录）",
            )

        cmd = [
            sys.executable,
            "-m",
            "http.server",
            str(deployment.port),
            "--bind",
            HOST,
            "--directory",
            workdir,
        ]
        deployment.command = " ".join(cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        deployment._process = proc
        deployment.pid = proc.pid
        deployment._log_task = asyncio.create_task(self._pump_logs(deployment, proc))

    async def _start_command(
        self,
        deployment: Deployment,
        command: str | None,
        env: dict | None,
    ) -> None:
        if not command or not str(command).strip():
            raise ValueError("command 模式必须提供 command 启动命令")

        # $PORT 替换 + 环境注入
        resolved_cmd = str(command).replace("$PORT", str(deployment.port))
        deployment.command = resolved_cmd

        sub_env = dict(os.environ)
        if env:
            sub_env.update({str(k): str(v) for k, v in env.items()})
        sub_env["PORT"] = str(deployment.port)

        proc = await asyncio.create_subprocess_shell(
            resolved_cmd,
            cwd=deployment.workdir,
            env=sub_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        deployment._process = proc
        deployment.pid = proc.pid
        deployment._log_task = asyncio.create_task(self._pump_logs(deployment, proc))

    async def _pump_logs(self, deployment: Deployment, proc: Any) -> None:
        """后台读取 stdout（已合并 stderr），保留尾部若干行。"""
        stream = proc.stdout
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                self._append_log(
                    deployment,
                    line.decode("utf-8", errors="replace").rstrip("\n"),
                )
        except Exception:
            # 读取被取消或进程退出，忽略
            pass

    def _append_log(self, deployment: Deployment, line: str) -> None:
        deployment.logs.append(line)
        if len(deployment.logs) > LOG_TAIL_LINES:
            del deployment.logs[: len(deployment.logs) - LOG_TAIL_LINES]

    # ── 内部：就绪轮询 ──────────────────────────────────────────────
    async def _wait_ready(self, deployment: Deployment) -> bool:
        deadline = time.time() + READY_TIMEOUT
        proc = deployment._process
        while time.time() < deadline:
            # 进程提前退出 -> 失败
            if proc is not None and proc.returncode is not None:
                return False
            if _tcp_can_connect(HOST, deployment.port, timeout=1.0):
                return True
            await asyncio.sleep(READY_POLL_INTERVAL)
        return False

    # ── 内部：进程组终止 ────────────────────────────────────────────
    async def _kill_process(self, deployment: Deployment) -> bool:
        proc = deployment._process
        pid = deployment.pid
        if proc is None and pid is None:
            return False

        success = False
        # 对整组发 SIGTERM
        if pid is not None:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                success = True
            except (ProcessLookupError, PermissionError, OSError):
                pass

        # 短暂等待退出
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                success = True
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

        # SIGKILL 兜底
        if pid is not None:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                success = True
            except (ProcessLookupError, PermissionError, OSError):
                pass

        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass

        # 取消日志读取任务
        log_task = deployment._log_task
        if log_task is not None and not log_task.done():
            log_task.cancel()

        return success

    # ── 内部：sweeper（闲置/超寿命回收）───────────────────────────────
    def _ensure_sweeper(self) -> None:
        """懒启动 sweeper。没有事件循环时安静返回，不崩。"""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无运行中的事件循环
        self._sweeper_task = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(SWEEP_INTERVAL)
                await self._sweep_once()
            except asyncio.CancelledError:
                break
            except Exception:
                # sweeper 自身错误不应让循环退出
                continue

    async def _sweep_once(self) -> None:
        now = time.time()
        for deployment in list(self._deployments.values()):
            if deployment.status != "running":
                continue
            idle = now - deployment.last_seen
            lifetime = now - deployment.created_at
            if idle > IDLE_TIMEOUT or lifetime > MAX_LIFETIME:
                await self.stop(deployment.deployment_id)


# ── 事件 payload 构造（native deploy 工具与 coding executor 共用，保证形状一致）──
def build_deploy_artifact(deployment: Deployment) -> dict[str, Any]:
    """契约 B 的 deploy artifact 对象。"""
    return {
        "type": "deploy",
        "title": deployment.title,
        "url": deployment.url,
        "port": deployment.port,
        "deployment_id": deployment.deployment_id,
        "status": deployment.status,
        "kind": deployment.kind,
        "command": deployment.command or "",
        "error": deployment.error or "",
        "metadata": {},
        "editable": False,
    }


def build_deploy_event_payload(
    deployment: Deployment,
    agent_id: str,
    run_id: str,
) -> dict[str, Any]:
    """契约 C 的 artifacts.deploy 事件 payload。"""
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "event_name": "artifacts.deploy",
        "frontend_event_name": "artifacts.deploy",
        "artifact_type": "deploy",
        "artifact": build_deploy_artifact(deployment),
        "created_at": time.time(),
    }


# 进程内模块级单例（契约 D）
deployment_manager = DeploymentManager()
