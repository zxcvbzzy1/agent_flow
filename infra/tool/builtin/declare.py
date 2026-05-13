from domain.tool import Tool

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
