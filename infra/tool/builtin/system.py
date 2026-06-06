from domain.event import Event
from domain.tool import Tool, Tool_respond
import subprocess
import os
import bashlex
from pathlib import Path
from typing import ClassVar, Optional
from infra.event_bind import On_bind
from infra.config import factory, agent_dict,bus
from infra.tool.common_func import human_approval_service


# —— bash 危险命令处理策略（可由工具页 PATCH 热更新；im_backend 与 agent_flow 同进程，set 即时生效）——
# danger_policy: "reject" 高危命令直接拒绝（原行为，作为可配置项保留）｜ "confirm" 转人工确认
# auto_confirm:  "ask" 弹窗询问用户 ｜ "approve" 自动批准 ｜ "reject" 自动拒绝（headless/测试用）
_BASH_SETTINGS: dict[str, str] = {"danger_policy": "reject", "auto_confirm": "ask"}


def get_bash_settings() -> dict:
    return dict(_BASH_SETTINGS)


def set_bash_settings(danger_policy: str | None = None, auto_confirm: str | None = None) -> dict:
    if danger_policy in {"reject", "confirm"}:
        _BASH_SETTINGS["danger_policy"] = danger_policy
    if auto_confirm in {"ask", "approve", "reject"}:
        _BASH_SETTINGS["auto_confirm"] = auto_confirm
    return get_bash_settings()


BASH = Tool(
    name="bash",
    description="""执行 bash 命令，执行前会审核高危命令和工作路径，返回 stdout、stderr 和退出码。
    当 `file` 工具（read/write/append/edit/apply_patch/list_dir/glob/search_text 等操作）无法满足需求时，可以使用该工具执行更复杂的文件操作命令。""",
    field="system",
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 bash 命令，例如 'cp a.txt b.txt'"
            }
        },
        "required": ["command"]
    },
    # 始终经人机协作中间件：human.bash 处理器对「安全命令」直接放行，仅对「高危命令」按
    # danger_policy 决定 拒绝 / 转人工确认。策略在处理器内运行时读取，可热更新。
    metadata={"require_human_confirm": True},
)

class SystemTool():
    HIGH_RISK_COMMANDS: ClassVar[set[str]] = {
        "rm",
        "sudo",
        "chmod",
        "chown",
        "dd",
        "mkfs",
        "shutdown",
        "reboot",
        "kill",
        "killall",
        "pkill",
    }
    SHELL_COMMANDS: ClassVar[set[str]] = {"bash", "sh", "zsh"}

    def __init__(self, working_directory: str | Path):
        self.working_directory = Path(working_directory).expanduser().resolve()

    # CLI命令高危检验
    def audit_high_risk_command(self, command: str) -> tuple[bool, str]:
        try:
            ast_parts = bashlex.parse(command)
        except (bashlex.errors.ParsingError, NotImplementedError):
            # bashlex 不支持 heredoc 等语法，解析失败不代表命令危险
            # 对原始命令文本做关键词兜底检查
            first_word = command.strip().split()[0] if command.strip() else ""
            first_word = os.path.basename(first_word).lower()
            if first_word in self.HIGH_RISK_COMMANDS:
                return False, f"命令包含高危指令 `{first_word}`，已拒绝执行"
            return True, ""

        for node in ast_parts:
            command_name = self._find_high_risk_command(node)
            if command_name:
                return False, f"命令包含高危指令 `{command_name}`，已拒绝执行"

        return True, ""

    def _find_high_risk_command(self, node) -> Optional[str]:
        if node.kind == "command":
            command_name = ""
            for part in getattr(node, "parts", []):
                if part.kind != "word":
                    continue
                if self._is_env_assignment(part.word):
                    continue
                command_name = os.path.basename(part.word)
                break
            if command_name.lower() in self.HIGH_RISK_COMMANDS:
                return command_name
            if command_name.lower() in self.SHELL_COMMANDS:
                words = [
                    part.word
                    for part in getattr(node, "parts", [])
                    if part.kind == "word"
                ]
                for index, word in enumerate(words):
                    if word == "-c" and index + 1 < len(words):
                        try:
                            nested_ast_parts = bashlex.parse(words[index + 1])
                        except bashlex.errors.ParsingError:
                            continue
                        for ast_part in nested_ast_parts:
                            nested_command = self._find_high_risk_command(ast_part)
                            if nested_command:
                                return nested_command

        for child in self._iter_child_nodes(node):
            command_name = self._find_high_risk_command(child)
            if command_name:
                return command_name
        return None

    def _iter_child_nodes(self, node):
        for attr in ("parts", "list"):
            for child in getattr(node, attr, []):
                yield child
        for attr in ("command", "output"):
            child = getattr(node, attr, None)
            if child is not None and hasattr(child, "kind"):
                yield child

    # 工作路径安全检验
    ALLOWED_DEVICE_PATHS: ClassVar[set[str]] = {"/dev/null", "/dev/zero", "/dev/urandom", "/dev/stdin", "/dev/stdout", "/dev/stderr"}

    def audit_working_directory(self, command: str) -> tuple[bool, str]:
        try:
            ast_parts = bashlex.parse(command)
        except (bashlex.errors.ParsingError, NotImplementedError):
            # bashlex 不支持 heredoc 等语法，解析失败时对路径做文本兜底检查
            for word in command.strip().split():
                if word in self.ALLOWED_DEVICE_PATHS:
                    continue
                if word.startswith(("/", "./", "../")) or ("/" in word and not word.startswith("-")):
                    path = Path(word).expanduser()
                    if path.is_absolute():
                        resolved = path.resolve()
                    else:
                        resolved = (self.working_directory / path).resolve()
                    try:
                        resolved.relative_to(self.working_directory)
                    except ValueError:
                        return False, f"命令路径 `{resolved}` 超出允许范围 `{self.working_directory}`，已拒绝执行"
            return True, ""

        def is_path_word(word: str) -> bool:
            if not word or "://" in word or word.startswith(("git@", "$")):
                return False
            if word in self.ALLOWED_DEVICE_PATHS:
                return False
            return (
                word.startswith("/")
                or word.startswith("./")
                or word.startswith("../")
                or word in {".", "..", "~"}
                or "/" in word
            )

        def resolve_path(word: str) -> Path:
            path = Path(word).expanduser()
            if path.is_absolute():
                return path.resolve()
            return (self.working_directory / path).resolve()

        def is_allowed_path(path: Path) -> bool:
            try:
                path.relative_to(self.working_directory)
                return True
            except ValueError:
                return False

        def extract_path_words(word: str) -> list[str]:
            if word.startswith("-") and "=" in word:
                _, value = word.split("=", 1)
                return [value] if is_path_word(value) else []
            if word.startswith("-"):
                return []
            return [word] if is_path_word(word) else []

        def find_unsafe_path(node) -> Optional[Path]:
            if node.kind == "command":
                command_seen = False
                command_name = ""
                words = []
                for part in getattr(node, "parts", []):
                    if part.kind != "word":
                        continue
                    words.append(part.word)
                    if self._is_env_assignment(part.word):
                        continue
                    if not command_seen:
                        command_seen = True
                        command_name = os.path.basename(part.word).lower()
                        continue
                    for word in extract_path_words(part.word):
                        path = resolve_path(word)
                        if not is_allowed_path(path):
                            return path

                if command_name in self.SHELL_COMMANDS:
                    for index, word in enumerate(words):
                        if word == "-c" and index + 1 < len(words):
                            try:
                                nested_ast_parts = bashlex.parse(words[index + 1])
                            except bashlex.errors.ParsingError:
                                continue
                            for ast_part in nested_ast_parts:
                                unsafe_path = find_unsafe_path(ast_part)
                                if unsafe_path:
                                    return unsafe_path

            if node.kind == "redirect":
                redirect_target = getattr(node, "output", None) or getattr(node, "input", None)
                if redirect_target is not None and redirect_target.kind == "word":
                    for word in extract_path_words(redirect_target.word):
                        path = resolve_path(word)
                        if not is_allowed_path(path):
                            return path

            for child in self._iter_child_nodes(node):
                unsafe_path = find_unsafe_path(child)
                if unsafe_path:
                    return unsafe_path
            return None

        for node in ast_parts:
            unsafe_path = find_unsafe_path(node)
            if unsafe_path:
                return False, f"命令路径 `{unsafe_path}` 超出允许范围 `{self.working_directory}`，已拒绝执行"

        return True, ""

    def _is_env_assignment(self, token: str) -> bool:
        name, separator, _ = token.partition("=")
        return (
            bool(separator)
            and bool(name)
            and name.replace("_", "").isalnum()
            and not name[0].isdigit()
        )

    #统一检验
    def audit_bash(self, command: str) -> tuple[bool, str, Path]:
        # 高危命令策略（保留原「直接拒绝」逻辑，改为参数配置 danger_policy）：
        # - reject：工具层直接拒绝高危命令（与人机协作中间件双重保障；即使中间件未生效也不放过）。
        # - confirm：是否执行由中间件按用户确认决定，这里放行，否则被用户确认通过的高危命令会被二次拒绝。
        if get_bash_settings().get("danger_policy", "reject") == "reject":
            allowed, reason = self.audit_high_risk_command(command)
            if not allowed:
                return False, reason, self.working_directory

        # 工作路径检验（当前关闭）
        # allowed, reason = self.audit_working_directory(command)
        # if not allowed:
        #     return False, reason, self.working_directory

        return True, "", self.working_directory


    def exec_bash(self, command: str):
        allowed, reason, target_dir = self.audit_bash(command)
        if not allowed:
            return {
                "stdout": "",
                "stderr": reason,
                "returncode": 126
            }

        result = subprocess.run(
            command,
            capture_output=True, # 捕获输出
            text=True,           # 转为字符串
            check=False,          # 不抛出异常，自己处理
            shell=True,         # 使用 shell 解析命令
            env=os.environ,     # 继承环境变量
            cwd=target_dir,
            timeout=30          # 防止挂死
        )
        if result.returncode != 0:
            print(f"Command failed with return code {result.returncode}")
            print(f"stderr: {result.stderr}")
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    

on_tool = On_bind()
factory._build_and_register_list([BASH], bus)


@on_tool.on(factory.tool("bash").called())
def exec_bash(**kwargs)->Event:
    command = kwargs["command"]
    agent_id = kwargs["agent_id"]
    work_path = agent_dict[agent_id].work_path
    tool = SystemTool(working_directory=work_path)
    respond = tool.exec_bash(command)
    if respond["returncode"] != 0:
        tool_respond = Tool_respond(
                agent_id=agent_id,
                name="bash",
                success=False,
                respond=f"命令执行失败: {respond['stderr']}"
            )
        return factory.tool("bash").failed(tool_respond)
    else:
        # 成功但无 stdout 的命令（mkdir/touch/写文件/cd 等）若回传空字符串，模型看不到结果会重复调用，
        # 这里兜底成明确的成功提示，保证 tool.succeeded 负载非空。
        stdout = respond.get("stdout") or ""
        tool_respond = Tool_respond(
                agent_id=agent_id,
                name="bash",
                success=True,
                respond=stdout if stdout.strip() else "命令执行成功，无标准输出（returncode=0）"
            )
        return factory.tool("bash").succeeded(tool_respond)

def _confirmed(approved: bool, reason: str) -> Event:
    return Event("human.bash.confirmed", payload={"approved": approved, "reason": reason})


@on_tool.on(Event("human.bash"))
async def confirm(**kwargs) -> Event:
    """人机协作中间件对每次 bash 调用都会经过这里。

    安全命令直接放行（不打扰用户、不发确认事件）；只有高危命令才按 danger_policy 决策：
    reject → 直接拒绝（原行为）；confirm → 按 auto_confirm 自动批准/拒绝，或弹窗询问用户。
    """
    arguments = kwargs.get("arguments", {})
    command = arguments.get("command", "")
    agent_id = arguments.get("agent_id", "")

    # 仅对高危命令需要决策；audit_high_risk_command 不依赖工作目录，传占位路径即可
    allowed, _reason = SystemTool(working_directory=".").audit_high_risk_command(command)
    if allowed:
        return _confirmed(True, "非高危命令，自动放行")

    settings = get_bash_settings()
    policy = settings.get("danger_policy", "reject")

    # 策略一：直接拒绝（原行为，保留为可配置项）
    if policy != "confirm":
        return _confirmed(False, f"高危命令按当前策略（{policy}）直接拒绝执行")

    # 策略二：人工确认。环境变量优先（headless），其次工具页 auto_confirm 配置
    env_auto = os.getenv("AGENT_FLOW_AUTO_CONFIRM", "").lower()
    auto = settings.get("auto_confirm", "ask")
    if env_auto in {"1", "true", "yes", "y"} or auto == "approve":
        return _confirmed(True, "高危命令自动批准（auto_confirm=approve）")
    if env_auto in {"0", "false", "no", "n"} or auto == "reject":
        return _confirmed(False, "高危命令自动拒绝（auto_confirm=reject）")

    # auto == "ask"：通过 human_approval_service 弹出 Web 确认（_human_input_context 已由中间件设置）
    print(f"\n[HUMAN CONFIRM] 高危 bash 命令请求执行：agent_id={agent_id} command={command}")
    answer = (
        await human_approval_service.input(f"高危命令待确认：{command}\n是否允许执行？输入 yes/y 允许，其它拒绝: ")
    ).strip().lower()
    approved = answer in {"yes", "y"}
    return _confirmed(
        approved,
        "用户确认执行高危命令" if approved else f"用户拒绝执行高危命令，{answer}",
    )
