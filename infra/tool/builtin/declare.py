from domain.tool import Tool


# 计划
WRITE_PLAN = Tool(
    name        = "write_plan",
    description = "根据用户要求，制定结构化任务计划，返回计划列表",
    input_schema = {
        "type": "object",
        "properties": {
            "user_prompt": {
                "type": "string",
                "description": "用户的原始要求"
            },
            "reference": {
                "type": "string",
                "description": "参考资料,如可使用的工具等"
            }
        },
        "required": ["user_prompt","reference"]
    },
    field="plan"
)

UPDATE_PLAN = Tool(
    name        = "update_plan",
    description = "更新已有计划中指定步骤的状态或内容，返回更新后的计划",
    input_schema = {
        "type": "object",
        "properties": {
            "step_id": {
                "type": "string",
                "description": "要更新的步骤 ID"
            },
            "title": {
                "type": "string",
                "description": "新标题"
            },
            "detail": {
                "type": "string",
                "description": "新详情"
            },
            "status": {
                "type": "string",
                "description": "步骤状态",
                "enum": ["pending", "in_progress", "done", "failed", "skipped"]
            },
            "note": {
                "type": "string",
                "description": "备注，如失败原因或完成说明"
            }
        },
        "required": ["step_id"]
    },
    field="plan"
)


FINISH_PLAN = Tool(
    name        = "finish_plan",
    description = "标记整个计划为完成状态，输出最终总结",
    input_schema = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "计划执行完成后的总结"
            }
        },
        "required": ["summary"]
    },
    field="plan"
)

# 搜索
# RAG_SEARCH = Tool( 
#     name        = "rag_search",
#     description = "检索存储到知识库里的文档。当用户提及根据上传的文档时使用，一般不使用",
#     input_schema = {
#         "type": "object",
#         "properties": {
#             "query": {
#                 "type":        "string",
#                 "description": "用户问题的概述，保留问题里与文档要求相关的部分"
#             }
#         },
#         "required": ["query"]
#     },
#     field="search"
#     )





#人工
CONFIRM_HUNMAN = Tool(
    name        = "confirm_human",
    description = "用户问题指向不确定时，向用户进行确认进一步的细节。",
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type":        "string",
                "description": "向用户确认的问题"
            },
        },
        "required": ["query"]
    },
    field = "human"
)

