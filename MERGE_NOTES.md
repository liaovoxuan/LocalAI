# LocalAI edition merge

- `local_ai.py` is now the single full implementation for Standard, Pro and Ultra.
- `local_ai_pro.py` and `LocalAI_Ultra.py` are compatibility launchers only.
- New installations start as Standard.
- Settings now contains **版本与激活**.
- Temporary offline activation rule: exactly 7 digits, with a digit sum of 54.
- Pro enables OpenAI-compatible providers and the Pro web feature profile.
- Ultra additionally enables the official OpenAI provider and the Ultra web feature profile.
- Invalid stored activation data automatically falls back to Standard.
