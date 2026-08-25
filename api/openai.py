# OpenAI
#   https://llm-api.net/v1
# Anthropic
#   https://llm-api.net
# Gemini
#   https://llm-api.net

# APIKEY: 

# POST https://llm-api.net/v1beta/models/gemini-3.1-pro-preview:generateContent（也可用 Authorization Bearer 或 URL ?key=）
import requests

url = "https://llm-api.net/v1/chat/completions"
api_key = "sk-pIXX3MnQ406ZJ0WBDIsRmxSse5ZfZUNDu3V09VV8Aiqk9jxX"

headers = {
    "x-goog-api-key": api_key,
    "Content-Type": "application/json",
}
body = {
    "contents": [
        {
            "role": "user", 
            "parts": [
                {"text": "你好，请用一句话介绍你自己。"}
            ]
        }
    ]
}
r = requests.post(url, headers=headers, json=body, timeout=120)
print(r.json())
