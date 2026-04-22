from domain.tool import Tool

# 内容创作agent
# 工具节点
#     - 大纲
#     - 续写
#     - 重写
#     - 润色
#     - 需求分析


# 需求分析工具
REQUIREMENTS_ANALYSIS = Tool(
    name="requirements_analysis",
    description="分析用户的创作需求，提取关键要素如题材、风格、角色设定、情节要求等，生成结构化的需求文档",
    input_schema={
        "type": "object",
        "properties": {
            "user_prompt": {
                "type": "string",
                "description": "用户的原始创作需求描述"
            }
        },
        "required": ["user_prompt"]
    },
    field="write_agent"
)

# 大纲生成工具
OUTLINE_GENERATION = Tool(
    name="outline_generation",
    description="根据用户需求生成故事大纲，包括章节划分、主要情节点、角色发展轨迹等",
    input_schema={
        "type": "object",
        "properties": {
            "requirements": {
                "type": "string",
                "description": "用户需求"
            },
            "chapter_count": {
                "type": "integer",
                "description": "期望的章节数量"
            },
            "total_words": {
                "type": "integer",
                "description": "目标总字数"
            }
        },
        "required": ["requirements"]
    },
    field="write_agent"
)


# 初稿写作工具（续写）
FIRST_DRAFT_WRITING = Tool(
    name="draft_writing",
    description="根据要求，生成初稿文本",
    input_schema={
        "type": "object",
        "properties": {
            "requirements": {
                "type": "string",
                "description": "用户要求"
            },
            "reference": {
                "type": "string",
                "description": "参考资料"
            }
        },
        "required": ["requirements"]
    },
    field="write_agent"
)


# 重写工具
REWRITE = Tool(
    name="rewrite",
    description="根据评判反馈对内容进行重写，改进故事情节、人物塑造或文笔表达",
    input_schema={
        "type": "object",
        "properties": {
            "original_content": {
                "type": "string",
                "description": "需要重写的原始内容"
            },
            "feedback": {
                "type": "string",
                "description": "评判给出的修改建议和反馈"
            },
            "score": {
                "type": "number",
                "description": "原始内容的评分"
            },
            "rewrite_focus": {
                "type": "string",
                "description": "重写重点，如情节逻辑、人物刻画、语言表达等"
            }
        },
        "required": ["original_content", "feedback"]
    },
    field="write_agent"
)

# 润色工具
STYLE_POLISH = Tool(
    name="style_polish",
    description="对高质量内容进行风格润色，优化语言表达、修辞手法和文学性",
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "需要润色的文本内容"
            },
            "style_preference": {
                "type": "string",
                "description": "润色要求"
            }
        },
        "required": ["content"]
    },
    field="write_agent"
)

# 需求检查工具
REQUIREMENTS_CHECK = Tool(
    name="requirements_check",
    description="检查生成的文本是否满足用户的需求，并给出相应的反馈",
    input_schema={
        "type": "object",
        "properties": {
            "requirements": {
                "type": "string",
                "description": "用户需求"
            },
            "content": {
                "type": "string",
                "description": "生成的文本内容"
            }
        },
        "required": ["requirements","content"]
    },
    field="write_agent"
)