import os
import json
from google import genai
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "prompts", "generate_frame_scene_graph.txt"
)

def generate_frame_scene_graph(image_path):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to a .env file in the project root "
            "or export it in the shell before running vidsgg.py."
        )

    client = genai.Client(api_key=api_key)
    with open(PROMPT_PATH, "r", encoding="utf-8") as file:
        prompt = file.read()

    image = Image.open(image_path)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[image, prompt])

    print(response.text)
    json_res = json.loads(response.text.replace('json', '').replace('```',''))
    return json_res
