"""Application presets. Limits include reasoning where the provider counts it."""
SCENARIOS = {
    "quality": {"label": "能力回归", "max_tokens": None, "timeout": 600, "profile": "short",
                "description": "6道推理题，接口控制输出额度，单题最长10分钟", "scheduled_only": True},
    "probe": {"label": "标准模型巡检", "max_tokens": 8192, "timeout": 180, "profile": "short",
              "description": "标准短输入、约180字回答，每轮1次，不自动重试", "scheduled_only": True},
    "chat": {"label": "日常聊天", "max_tokens": 8192, "timeout": 180, "profile": "short",
             "description": "简短业务问答，预留思考与正文空间"},
    "knowledge": {"label": "知识库问答", "max_tokens": 16384, "timeout": 300, "profile": "long_input",
                  "description": "基于给定材料回答，模拟知识库问答的模型调用"},
    "workflow": {"label": "智能体工作流", "max_tokens": 32768, "timeout": 480, "profile": "long_output",
                 "description": "计划与结构化输出，模拟工作流中的单次模型调用"},
}


def apply_scenario(config):
    result = dict(config)
    scenario = "quality" if result.get("suite") == "quality" else result.get("scenario", "chat")
    preset = SCENARIOS[scenario]
    result.update(scenario=scenario, scenario_label=preset["label"],
                  max_tokens=preset["max_tokens"], timeout=preset["timeout"], temperature=None)
    if result.get("suite") != "quality" and result.get("profile") != "custom" and not result.get("prompt") and not result.get("extra", {}).get("messages"):
        result["profile"] = preset["profile"]
    return result
