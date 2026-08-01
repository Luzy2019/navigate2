import os 
import random
import time
from typing import List
from google import genai
from google.genai import types
from openai import OpenAI 

from og_ego_prim.models.base_client import BaseClient
from og_ego_prim.models.image_utils import (
    encode_image, 
    guess_image_type_from_base64,
)
from og_ego_prim.models.openai_config import get_openai_request_kwargs


def _openai_request_timeout_seconds() -> float:
    raw_value = os.environ.get("ISBENCH_OPENAI_REQUEST_TIMEOUT_SECONDS", "180")
    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            "ISBENCH_OPENAI_REQUEST_TIMEOUT_SECONDS must be a positive number"
        ) from exc
    if timeout <= 0:
        raise ValueError("ISBENCH_OPENAI_REQUEST_TIMEOUT_SECONDS must be a positive number")
    return timeout


def read_image(image_path: str):
  with open(image_path, "rb") as f:
     return f.read()
  
class ServerClient(BaseClient):

    def __init__(self, model_type, model_name, api_key=os.environ.get("OPENAI_API_KEY"), api_base=os.environ.get("OPENAI_API_BASE")) -> None:
        self.model_type = model_type
        if model_type == "local":
            for proxy_var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
                os.environ.pop(proxy_var, None)

        if 'gemini_direct' in model_name.lower():
            self.client = genai.Client(
                vertexai=True,
                # project="czby-gemini-250612",
                location="global",
            )
            self.generate_content_config = types.GenerateContentConfig(
                temperature = 1,
                top_p = 1,
                seed = 0,
                max_output_tokens = 65535,
                safety_settings = [types.SafetySetting(
                category="HARM_CATEGORY_HATE_SPEECH",
                threshold="OFF"
                ),types.SafetySetting(
                category="HARM_CATEGORY_DANGEROUS_CONTENT",
                threshold="OFF"
                ),types.SafetySetting(
                category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                threshold="OFF"
                ),types.SafetySetting(
                category="HARM_CATEGORY_HARASSMENT",
                threshold="OFF"
                )],
            )
        else:
            self.client = OpenAI(
                api_key=api_key,
                base_url=api_base,
                timeout=_openai_request_timeout_seconds(),
                max_retries=0,
            )
        
        if model_name == "local":
            model_name = self.client.models.list().data[0].id
        self.model_name = model_name
        print(f"MODEL NAME: {self.model_name}")
        
    def model(self, prompt, image_file: List[str] | str = None, gen_args=None):
        if gen_args is None:
            gen_args = {"max_completion_tokens": 512, "temperature": 0.0}

        if isinstance(image_file, str):  # support single and multi image
            image_file = [image_file]
        image_file = image_file or []
        last_error = None

        if 'gemini_direct' in self.model_name.lower(): # support gemini api
            parts = [types.Part.from_text(text=prompt)]
            for image in image_file:
                image_base64 = read_image(image)
                image_content = types.Part.from_bytes(
                    data=image_base64,
                    mime_type="image/png",
                )
                parts.append(image_content)
            contents = [
                types.Content(
                    role="user",
                    parts=parts
                )
            ]

            for _ in range(3):
                result = ""
                try:
                    for chunk in self.client.models.generate_content_stream(
                        model=self.model_name,
                        contents=contents,
                        config=self.generate_content_config,
                    ):
                        result += chunk.text
                    if result.strip():
                        return result
                except Exception as e:
                    last_error = e
                    print(e)
                    time.sleep(10)
            raise RuntimeError("Gemini model returned no content after 3 attempts") from last_error

        if image_file:
            content = [
                {
                    "type": "text",
                    "text": prompt
                },
            ]
            for image in image_file:
                image_base64 = encode_image(image)
                image_type = guess_image_type_from_base64(image_base64)
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_type};base64,{image_base64}"
                        },
                    }
                )
        else:
            content = prompt
        messages = [
            {
                "role": "user",
                "content": content
            }
        ]

        for _ in range(3):
            try:
                request_kwargs = {}
                if self.model_type != "local":
                    request_kwargs = get_openai_request_kwargs()
                chat_completion = self.client.chat.completions.create(
                    messages=messages,
                    model=self.model_name,
                    **gen_args,
                    **request_kwargs,
                )
                result = chat_completion.choices[0].message.content
                if result and result.strip():   # 避免none或空白响应
                    return result
            except Exception as e:
                last_error = e
                print(e)
                time.sleep(10)
        raise RuntimeError("OpenAI model returned no content after 3 attempts") from last_error
