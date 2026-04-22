from domain.agent_base import AgentBase
from domain.event import Tool_respond, Event
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.tool.tool_bind import Tool_bind
import os
from pathlib import Path
from infra.config import factory,agent_dict,llm_client


# factory = StaticToolEventFactory(prefix="infra") #编写时方便
on_tool = Tool_bind()


# 中间件
@on_tool.use()
async def logging_middleware(event: Event, call_next):
    print(f"[LOG] 事件触发: {event.name}")
    print(f"{event.name}负载为:\n {event.payload}")

    try:
        result = await call_next()
        print(f"[LOG] 事件完成: {event.name}")
        return result

    except Exception as e:
        print(f"[ERROR] 事件异常: {event.name} -> {e}")
        raise   # ⚠️ 一定要 rethrow



# 成功/失败返回事件
@on_tool.on_pattern("*.succeeded") 
async def on_tool_success(**kwargs):
    agent_id = kwargs.get("agent_id")
    agent = agent_dict.get(agent_id)
    await agent.on_tool_call(
        tool_name=kwargs.get("name"),
        success=True,
        respond=kwargs.get("respond"),
    )
    
@on_tool.on_pattern("*.failed")
async def on_tool_fail(**kwargs):  # event.playload为Tool_respond类，kwargs为Tool_respond类解构可字典调用
    agent_id = kwargs.get("agent_id")
    agent = agent_dict.get(agent_id)
    await agent.on_tool_call(
        tool_name=kwargs.get("name"),
        success=False,
        respond=kwargs.get("respond"),
    )

@on_tool.on(factory.tool("query_tool_respond").succeeded({}))
async def on_query_tool_respond_tool_successed(**kwargs):  
    agent_id = kwargs.get("agent_id")
    agent:AgentBase = agent_dict.get(agent_id)
    full_respond = kwargs.get("respond")
    def callBack(respond,s) -> str:
        summary = f"{full_respond}" 
        return summary           
    await agent.on_tool_call(
        tool_name=kwargs.get("name"),
        success=True,
        respond=kwargs.get("respond"),
        callBack=callBack
    )



async def call_llm_with_prompt(system_prompt: str, user_prompt: str) -> str:
    """
    异步调用 LLM 生成内容的辅助函数
    
    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        max_tokens: 最大生成长度
        
    Returns:
        str: LLM 生成的文本内容
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    try:
        response_chunks = []
        async for chunk in llm_client.default_call(
            messages=messages,
            model=llm_client.model,
        ):
            response_chunks.append(chunk)
            print(chunk)
        
        full_response = "".join(response_chunks)
        return full_response.strip()
        
    except Exception as e:
        raise Exception(f"LLM 调用失败: {str(e)}")

# 系统工具
@on_tool.on(factory.tool("read_files").called())
def read_files(**kwargs) -> Event:
    """
    读取系统目录中的文件（包括用户上传文件）。
    
    Args:
        kwargs: 包含 file_path 列表的字典
        
    Returns:
        Event: 工具执行结果事件，负载为 Tool_respond 格式
    """
    try:
        file_paths = kwargs.get("file_path", [])
        agent_id = kwargs.get("agent_id", "")
        
        if not file_paths:
            respond = Tool_respond(
                agent_id=agent_id,
                name="read_files",
                success=False,
                respond="错误：未提供文件路径"
            )
            return factory.tool("read_files").failed(respond)
        
        results = {}
        errors = []
        
        for file_path in file_paths:
            try:
                path = Path(file_path)
                
                # 安全检查：防止路径遍历攻击
                base_dir = Path.cwd()
                resolved_path = path.resolve()
                
                if not str(resolved_path).startswith(str(base_dir)):
                    errors.append(f"禁止访问路径: {file_path} (超出工作目录范围)")
                    continue
                
                # 检查文件是否存在
                if not resolved_path.exists():
                    errors.append(f"文件不存在: {file_path}")
                    continue
                
                # 检查是否为文件
                if not resolved_path.is_file():
                    errors.append(f"路径不是文件: {file_path}")
                    continue
                
                # 读取文件内容
                try:
                    content = resolved_path.read_text(encoding='utf-8')
                    results[file_path] = {
                        "success": True,
                        "content": content,
                        "size": len(content),
                        "path": str(resolved_path)
                    }
                except UnicodeDecodeError:
                    # 如果是二进制文件，尝试以二进制模式读取
                    content_bytes = resolved_path.read_bytes()
                    results[file_path] = {
                        "success": True,
                        "content": f"<二进制文件，大小: {len(content_bytes)} bytes>",
                        "size": len(content_bytes),
                        "path": str(resolved_path),
                        "is_binary": True
                    }
                    
            except PermissionError:
                errors.append(f"权限不足，无法读取: {file_path}")
            except Exception as e:
                errors.append(f"读取文件失败 {file_path}: {str(e)}")
        
        # 构建响应
        if results:
            respond = Tool_respond(
                agent_id=agent_id,
                name="read_files",
                success=True,
                respond={
                    "message": f"成功读取 {len(results)} 个文件",
                    "files": results,
                    "errors": errors if errors else None
                }
            )
            return factory.tool("read_files").succeeded(respond)
        else:
            respond = Tool_respond(
                agent_id=agent_id,
                name="read_files",
                success=False,
                respond={
                    "message": "所有文件读取失败",
                    "errors": errors
                }
            )
            return factory.tool("read_files").failed(respond)
            
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="read_files",
            success=False,
            respond=f"工具执行异常: {str(e)}"
        )
        return factory.tool("read_files").failed(respond)


@on_tool.on(factory.tool("write_files").called())
def write_files(**kwargs) -> Event:
    """
    将内容写入系统目录中的文件。
    
    Args:
        kwargs: 包含 file_path 和 content 的字典
        
    Returns:
        Event: 工具执行结果事件，负载为 Tool_respond 格式
    """
    try:
        file_path = kwargs.get("file_path")
        content = kwargs.get("content", "")
        agent_id = kwargs.get("agent_id", "")
        
        if not file_path:
            respond = Tool_respond(
                agent_id=agent_id,
                name="write_files",
                success=False,
                respond="错误：未提供文件路径"
            )
            return factory.tool("write_files").failed(respond)
        
        if content is None:
            content = ""
        
        path = Path(file_path)
        
        # 安全检查：防止路径遍历攻击
        base_dir = Path.cwd()
        resolved_path = path.resolve()
        
        if not str(resolved_path).startswith(str(base_dir)):
            respond = Tool_respond(
                agent_id=agent_id,
                name="write_files",
                success=False,
                respond=f"禁止写入路径: {file_path} (超出工作目录范围)"
            )
            return factory.tool("write_files").failed(respond)
        
        # 确保父目录存在
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 写入文件
        try:
            resolved_path.write_text(content, encoding='utf-8')
            
            respond = Tool_respond(
                agent_id=agent_id,
                name="write_files",
                success=True,
                respond={
                    "message": "文件写入成功",
                    "file_path": str(resolved_path),
                    "content_size": len(content),
                    "encoding": "utf-8"
                }
            )
            return factory.tool("write_files").succeeded(respond)
            
        except PermissionError:
            respond = Tool_respond(
                agent_id=agent_id,
                name="write_files",
                success=False,
                respond=f"权限不足，无法写入: {file_path}"
            )
            return factory.tool("write_files").failed(respond)
            
        except OSError as e:
            respond = Tool_respond(
                agent_id=agent_id,
                name="write_files",
                success=False,
                respond=f"文件系统错误: {str(e)}"
            )
            return factory.tool("write_files").failed(respond)
            
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="write_files",
            success=False,
            respond=f"工具执行异常: {str(e)}"
        )
        return factory.tool("write_files").failed(respond)



# memory 工具实现
@on_tool.on(factory.tool("query_tool_respond").called())
async def query_tool_respond(**kwargs):
    agent_id = kwargs.get("agent_id")
    agent = agent_dict.get(agent_id)
    tool_name = kwargs.get("tool_name")
    index = kwargs.get("index",None)
    if agent is None:
        respond = Tool_respond(
            name="query_tool_respond", agent_id=agent_id,
            success=False, respond=f"Agent {agent_id} 不存在"
        )
        return factory.tool("query_tool_respond").failed(respond)

    full_store = agent.states.get("tool_respond_full", {})
    history = full_store.get(tool_name)

    if not history:
        respond = Tool_respond(
            name="query_tool_respond", agent_id=agent_id,
            success=False,
            respond=f"未找到工具 '{tool_name}' 的历史记录，已调用的工具：{list(full_store.keys())}"
        )
        return factory.tool("query_tool_respond").failed(respond)

    # index 不填返回最后一次
    if index is None:
        result = history[-1]
        desc = f"工具 '{tool_name}' 最后一次调用结果"
    else:
        if index < 1 or index > len(history):
            respond = Tool_respond(
                name="query_tool_respond", agent_id=agent_id,
                success=False,
                respond=f"index 超出范围，'{tool_name}' 共调用了 {len(history)} 次"
            )
            return factory.tool("query_tool_respond").failed(respond)
        result = history[index - 1]
        desc = f"工具 '{tool_name}' 第 {index} 次调用结果"

    respond = Tool_respond(
        name="query_tool_respond", agent_id=agent_id,
        success=True,
        respond=f"{desc}：\n{result}"
    )
    return factory.tool("query_tool_respond").succeeded(respond)


# ... write agent 工具实现...
@on_tool.on(factory.tool("requirements_analysis").called())
async def requirements_analysis(**kwargs) -> Event:
    """
    分析用户的创作需求，提取关键要素如题材、风格、角色设定、情节要求等，生成结构化的需求文档
    """
    try:
        user_prompt = kwargs.get("user_prompt", "")
        agent_id = kwargs.get("agent_id", "")
        print(f"{agent_id}正在执行需求分析工具...")
        if not user_prompt:
            respond = Tool_respond(
                agent_id=agent_id,
                name="requirements_analysis",
                success=False,
                respond="错误：未提供用户需求描述"
            )
            print(f"错误：未提供用户需求描述")
            return factory.tool("requirements_analysis").failed(respond)
        
        system_prompt = f"""你是一个专业的小说创作需求分析师。
你的任务是分析用户的创作需求，提取关键要素并生成结构化的需求文档。
请从以下维度进行分析：

1. 故事风格
2. 主要角色设定
3. 核心情节要求
4. 叙事视角和语气
5. 目标读者群体
6. 特殊要求或禁忌

"""

        user_message = f"""请分析以下创作需求：

{user_prompt}

请生成详细的需求分析文档。"""

        result = await call_llm_with_prompt(system_prompt, user_message)
        
        respond = Tool_respond(
            agent_id=agent_id,
            name="requirements_analysis",
            success=True,
            respond=result,
        )
        return factory.tool("requirements_analysis").succeeded(respond)
        
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="requirements_analysis",
            success=False,
            respond=f"需求分析失败: {str(e)}"
        )
        print(f"sb glm. {e}")
        return factory.tool("requirements_analysis").failed(respond)


@on_tool.on(factory.tool("outline_generation").called())
async def outline_generation(**kwargs) -> Event:
    """
    根据用户需求生成故事大纲，包括章节划分、主要情节点、角色发展轨迹等
    """
    try:
        requirements = kwargs.get("requirements", "")
        chapter_count = kwargs.get("chapter_count", 3)
        total_words = kwargs.get("total_words", 50000)
        agent_id = kwargs.get("agent_id", "")
        
        if not requirements:
            respond = Tool_respond(
                agent_id=agent_id,
                name="outline_generation",
                success=False,
                respond="错误：未提供需求文档"
            )
            return factory.tool("outline_generation").failed(respond)
        
        system_prompt = f"""你是一个专业的小说大纲规划师。
你的任务是根据用户需求生成详细的故事大纲。

要求：
- 规划 {chapter_count} 个章节
- 总字数目标：{total_words} 字
- 每章约 {total_words // chapter_count} 字

"""

        user_message = f"""请根据以下需求生成故事大纲：

{requirements}

请生成详细的章节大纲。"""

        result = await call_llm_with_prompt(system_prompt, user_message)
        
        respond = Tool_respond(
            agent_id=agent_id,
            name="outline_generation",
            success=True,
            respond=result,
        )
        return factory.tool("outline_generation").succeeded(respond)
        
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="outline_generation",
            success=False,
            respond=f"大纲生成失败: {str(e)}"
        )
        return factory.tool("outline_generation").failed(respond)


@on_tool.on(factory.tool("draft_writing").called())
async def first_draft_writing(**kwargs) -> Event:
    """
    根据要求，生成初稿文本
    """
    try:
        requirements = kwargs.get("requirements", "")
        reference = kwargs.get("reference", "")
        agent_id = kwargs.get("agent_id", "")
        
        if not requirements:
            respond = Tool_respond(
                agent_id=agent_id,
                name="first_draft_writing",
                success=False,
                respond="错误：未提供写作要求"
            )
            return factory.tool("first_draft_writing").failed(respond)
        
        system_prompt = """你是一个专业的小说作家。
你的任务是根据要求创作高质量的文本。
"""

        user_message = f"""请根据以下要求创作文本：

写作要求：
{requirements}

参考资料：
{reference if reference else "无"}

请开始创作。"""

        result = await call_llm_with_prompt(system_prompt, user_message)
        
        respond = Tool_respond(
            agent_id=agent_id,
            name="first_draft_writing",
            success=True,
            respond=result,
        )
        return factory.tool("first_draft_writing").succeeded(respond)
        
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="first_draft_writing",
            success=False,
            respond=f"初稿写作失败: {str(e)}"
        )
        return factory.tool("first_draft_writing").failed(respond)


@on_tool.on(factory.tool("rewrite").called())
async def rewrite(**kwargs) -> Event:
    """
    根据评判反馈对内容进行重写，改进故事情节、人物塑造或文笔表达
    """
    try:
        original_content = kwargs.get("original_content", "")
        feedback = kwargs.get("feedback", "")
        score = kwargs.get("score", 0)
        rewrite_focus = kwargs.get("rewrite_focus", "全面改进")
        agent_id = kwargs.get("agent_id", "")
        
        if not original_content or not feedback:
            respond = Tool_respond(
                agent_id=agent_id,
                name="rewrite",
                success=False,
                respond="错误：缺少原始内容或反馈意见"
            )
            return factory.tool("rewrite").failed(respond)
        
        system_prompt = f"""你是一个专业的小说编辑和改写专家。
你的任务是根据评判反馈对内容进行重写和改进。

当前评分：{score}
重写重点：{rewrite_focus}


请输出重写后的完整内容。"""

        user_message = f"""原始内容：
{original_content}

评判反馈：
{feedback}

请根据反馈进行重写，重点关注：{rewrite_focus}"""

        result = await call_llm_with_prompt(system_prompt, user_message)
        
        respond = Tool_respond(
            agent_id=agent_id,
            name="rewrite",
            success=True,
            respond= result,
                
        )
        return factory.tool("rewrite").succeeded(respond)
        
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="rewrite",
            success=False,
            respond=f"重写失败: {str(e)}"
        )
        return factory.tool("rewrite").failed(respond)


@on_tool.on(factory.tool("style_polish").called())
async def style_polish(**kwargs) -> Event:
    """
    对高质量内容进行风格润色，优化语言表达、修辞手法和文学性
    """
    try:
        content = kwargs.get("content", "")
        style_preference = kwargs.get("style_preference", "优美流畅")
        agent_id = kwargs.get("agent_id", "")
        
        if not content:
            respond = Tool_respond(
                agent_id=agent_id,
                name="style_polish",
                success=False,
                respond="错误：未提供需要润色的内容"
            )
            return factory.tool("style_polish").failed(respond)
        
        system_prompt = f"""你是一个专业的文学编辑和语言润色专家。
你的任务是对高质量内容进行风格润色，提升文学性。

润色风格：{style_preference}

"""

        user_message = f"""请对以下内容进行风格润色：

{content}

润色要求：{style_preference}"""

        result = await call_llm_with_prompt(system_prompt, user_message)
        
        respond = Tool_respond(
            agent_id=agent_id,
            name="style_polish",
            success=True,
            respond= result,
        )
        return factory.tool("style_polish").succeeded(respond)
        
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="style_polish",
            success=False,
            respond=f"润色失败: {str(e)}"
        )
        return factory.tool("style_polish").failed(respond)


@on_tool.on(factory.tool("requirements_check").called())
async def requirements_check(**kwargs) -> Event:
    """
    检查生成的文本是否满足用户的需求，并给出相应的反馈
    """
    try:
        requirements = kwargs.get("requirements", "")
        content = kwargs.get("content", "")
        agent_id = kwargs.get("agent_id", "")
        
        if not requirements or not content:
            respond = Tool_respond(
                agent_id=agent_id,
                name="requirements_check",
                success=False,
                respond="错误：缺少需求或内容"
            )
            return factory.tool("requirements_check").failed(respond)
        
        system_prompt = """你是一个严格的内容质量评估专家。
你的任务是检查生成的文本是否满足用户需求，并给出详细的评估反馈。

评估维度：
1. 需求符合度：是否满足用户的核心要求
2. 内容完整性：是否有遗漏的重要元素
3. 逻辑一致性：情节和人物是否合理
4. 语言表达：文笔是否流畅、准确
5. 创意性：是否有亮点和新意
6. 可读性：是否易于理解和接受

请给出：
- 综合评分（0-1之间的小数）
- 各项维度的详细评价
- 具体的改进建议
- 是否通过检查（pass/fail）

"""

        user_message = f"""需求文档：
{requirements}

生成内容：
{content}

请进行全面的检查和评估。"""

        result = await call_llm_with_prompt(system_prompt, user_message)
        
        respond = Tool_respond(
            agent_id=agent_id,
            name="requirements_check",
            success=True,
            respond=result,
        )
        return factory.tool("requirements_check").succeeded(respond)
        
    except Exception as e:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="requirements_check",
            success=False,
            respond=f"需求检查失败: {str(e)}"
        )
        return factory.tool("requirements_check").failed(respond)
    

