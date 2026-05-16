from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory, llm_client
from infra.event_bind import On_bind


REQUIREMENTS_ANALYSIS = Tool(
    name="requirements_analysis",
    description="分析用户的创作需求，提取关键要素如题材、风格、角色设定、情节要求等，生成结构化的需求文档",
    input_schema={
        "type": "object",
        "properties": {
            "user_prompt": {
                "type": "string",
                "description": "用户的原始创作需求描述",
            }
        },
        "required": ["user_prompt"],
    },
    field="write_agent",
)

OUTLINE_GENERATION = Tool(
    name="outline_generation",
    description="根据用户需求生成故事大纲，包括章节划分、主要情节点、角色发展轨迹等",
    input_schema={
        "type": "object",
        "properties": {
            "requirements": {
                "type": "string",
                "description": "为 requirements_analysis 的返回值",
            },
            "chapter_count": {
                "type": "integer",
                "description": "期望的章节数量",
            },
            "total_words": {
                "type": "integer",
                "description": "目标总字数",
            },
        },
        "required": ["requirements"],
    },
    field="write_agent",
)

FIRST_DRAFT_WRITING = Tool(
    name="draft_writing",
    description="根据要求，生成初稿文本",
    input_schema={
        "type": "object",
        "properties": {
            "requirements": {
                "type": "string",
                "description": "用户要求",
            },
            "reference": {
                "type": "string",
                "description": "参考资料",
            },
        },
        "required": ["requirements"],
    },
    field="write_agent",
)

REWRITE = Tool(
    name="rewrite",
    description="根据评判反馈对内容进行重写，改进故事情节、人物塑造或文笔表达",
    input_schema={
        "type": "object",
        "properties": {
            "original_content": {
                "type": "string",
                "description": "需要重写的原始内容",
            },
            "feedback": {
                "type": "string",
                "description": "评判给出的修改建议和反馈",
            },
            "score": {
                "type": "number",
                "description": "原始内容的评分",
            },
            "rewrite_focus": {
                "type": "string",
                "description": "重写重点，如情节逻辑、人物刻画、语言表达等",
            },
        },
        "required": ["original_content", "feedback"],
    },
    field="write_agent",
)

STYLE_POLISH = Tool(
    name="style_polish",
    description="对高质量内容进行风格润色，优化语言表达、修辞手法和文学性",
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "需要润色的文本内容",
            },
            "style_preference": {
                "type": "string",
                "description": "润色要求",
            },
        },
        "required": ["content"],
    },
    field="write_agent",
)

REQUIREMENTS_CHECK = Tool(
    name="requirements_check",
    description="检查生成的文本是否满足用户的需求，并给出相应的反馈",
    input_schema={
        "type": "object",
        "properties": {
            "requirements": {
                "type": "string",
                "description": "用户需求",
            },
            "content": {
                "type": "string",
                "description": "生成的文本内容",
            },
        },
        "required": ["requirements", "content"],
    },
    field="write_agent",
)


on_tool = On_bind()
factory._build_and_register_list(
    [
        REQUIREMENTS_ANALYSIS,
        OUTLINE_GENERATION,
        FIRST_DRAFT_WRITING,
        REWRITE,
        STYLE_POLISH,
        REQUIREMENTS_CHECK,
    ],
    bus,
)


async def call_llm_with_prompt(system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response_chunks = []
        async for chunk in llm_client.default_call(
            messages=messages,
            model=llm_client.model,
        ):
            response_chunks.append(chunk)
        return "".join(response_chunks).strip()
    except Exception as exc:
        raise Exception(f"LLM 调用失败: {exc}") from exc


@on_tool.on(factory.tool("requirements_analysis").called())
async def requirements_analysis(**kwargs) -> Event:
    try:
        user_prompt = kwargs.get("user_prompt", "")
        agent_id = kwargs.get("agent_id", "")
        print(f"{agent_id}正在执行需求分析工具...")
        if not user_prompt:
            respond = Tool_respond(
                agent_id=agent_id,
                name="requirements_analysis",
                success=False,
                respond="错误：未提供用户需求描述",
            )
            return factory.tool("requirements_analysis").failed(respond)

        system_prompt = """你是一个专业的小说创作需求分析师。
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
    except Exception as exc:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="requirements_analysis",
            success=False,
            respond=f"需求分析失败: {exc}",
        )
        return factory.tool("requirements_analysis").failed(respond)


@on_tool.on(factory.tool("outline_generation").called())
async def outline_generation(**kwargs) -> Event:
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
                respond="错误：未提供需求文档",
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
    except Exception as exc:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="outline_generation",
            success=False,
            respond=f"大纲生成失败: {exc}",
        )
        return factory.tool("outline_generation").failed(respond)


@on_tool.on(factory.tool("draft_writing").called())
async def draft_writing(**kwargs) -> Event:
    try:
        requirements = kwargs.get("requirements", "")
        reference = kwargs.get("reference", "")
        agent_id = kwargs.get("agent_id", "")

        if not requirements:
            respond = Tool_respond(
                agent_id=agent_id,
                name="draft_writing",
                success=False,
                respond="错误：未提供写作要求",
            )
            return factory.tool("draft_writing").failed(respond)

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
            name="draft_writing",
            success=True,
            respond=result,
        )
        return factory.tool("draft_writing").succeeded(respond)
    except Exception as exc:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="draft_writing",
            success=False,
            respond=f"初稿写作失败: {exc}",
        )
        return factory.tool("draft_writing").failed(respond)


@on_tool.on(factory.tool("rewrite").called())
async def rewrite(**kwargs) -> Event:
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
                respond="错误：缺少原始内容或反馈意见",
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
            respond=result,
        )
        return factory.tool("rewrite").succeeded(respond)
    except Exception as exc:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="rewrite",
            success=False,
            respond=f"重写失败: {exc}",
        )
        return factory.tool("rewrite").failed(respond)


@on_tool.on(factory.tool("style_polish").called())
async def style_polish(**kwargs) -> Event:
    try:
        content = kwargs.get("content", "")
        style_preference = kwargs.get("style_preference", "优美流畅")
        agent_id = kwargs.get("agent_id", "")

        if not content:
            respond = Tool_respond(
                agent_id=agent_id,
                name="style_polish",
                success=False,
                respond="错误：未提供需要润色的内容",
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
            respond=result,
        )
        return factory.tool("style_polish").succeeded(respond)
    except Exception as exc:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="style_polish",
            success=False,
            respond=f"润色失败: {exc}",
        )
        return factory.tool("style_polish").failed(respond)


@on_tool.on(factory.tool("requirements_check").called())
async def requirements_check(**kwargs) -> Event:
    try:
        requirements = kwargs.get("requirements", "")
        content = kwargs.get("content", "")
        agent_id = kwargs.get("agent_id", "")

        if not requirements or not content:
            respond = Tool_respond(
                agent_id=agent_id,
                name="requirements_check",
                success=False,
                respond="错误：缺少需求或内容",
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
    except Exception as exc:
        agent_id = kwargs.get("agent_id", "")
        respond = Tool_respond(
            agent_id=agent_id,
            name="requirements_check",
            success=False,
            respond=f"需求检查失败: {exc}",
        )
        return factory.tool("requirements_check").failed(respond)
