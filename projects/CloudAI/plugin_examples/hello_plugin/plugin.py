def register(api):
    api.register_action("显示问候", show_greeting, "Show a message from the plugin")
    api.register_tool(
        "reverse_text",
        reverse_text,
        description="Reverse the supplied text",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )


def show_greeting(arguments, context):
    context.notify("Hello from CloudAI")


def reverse_text(arguments, context):
    return {"ok": True, "text": str(arguments.get("text", ""))[::-1]}
