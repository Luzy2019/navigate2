from openai import OpenAI

client = OpenAI(api_key="sk-pIXX3MnQ406ZJ0WBDIsRmxSse5ZfZUNDu3V09VV8Aiqk9jxX", base_url="https://llm-api.net/v1")

stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "用通俗易懂的语言解释量子纠缠"}],
    stream=True,
    extra_body={"provider": {"sort": "success_rate"}},
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
