import base64
import os

from openai import OpenAI
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

MODEL = "gpt-4o-mini"
BASE_URL = "https://llm-api.net/v1"
OPENAI_API_KEY = "sk-jpLM9oMyLTJQp9y41T1BMh4aavOPADItJADH1riFqUikgLQN"
PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")


class SceneObject(BaseModel):
    id: int
    name: str
    bbox: list[int]
    is_hand: bool
    is_moving: bool


class Relationship(BaseModel):
    subj_id: int
    obj_id: int
    predicate: str


class SceneGraph(BaseModel):
    objects: list[SceneObject]
    relationships: list[Relationship]


def get_client():
    api_key = OPENAI_API_KEY
    base_url = BASE_URL
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to a .env file in the project root "
            "or export it in the shell before running vidsgg.py."
        )
    return OpenAI(api_key=api_key, base_url=base_url)


def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def get_image_media_type(image_path):
    extension = os.path.splitext(image_path)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(extension, "image/jpeg")


def load_prompt(filename):
    with open(os.path.join(PROMPT_DIR, filename), "r", encoding="utf-8") as file:
        return file.read()


def generate_frame_scene_graph(image_path):
    client = get_client()
    image_data = encode_image(image_path)
    media_type = get_image_media_type(image_path)
    prompt = load_prompt("generate_frame_scene_graph.txt")

    completion = client.chat.completions.parse(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_data}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        response_format=SceneGraph,
    )

    message = completion.choices[0].message
    if message.refusal:
        raise RuntimeError(f"GPT-4o refused the scene graph request: {message.refusal}")
    if message.parsed is None:
        raise RuntimeError("GPT-4o returned no structured scene graph.")

    scene_graph = message.parsed.model_dump()
    print(scene_graph)
    return scene_graph

def classify_and_describe_mask(image_path, mask_path):
    client = get_client()
    model_input = []
    model_input.append({
        "type": "image_url",
        "image_url": {
            "url":  f"data:image/jpeg;base64,{encode_image(image_path)}"
        },
        })
    model_input.append({
        "type": "image_url",
        "image_url": {
            "url":  f"data:image/jpeg;base64,{encode_image(mask_path)}"
        },
        })
    
    prompt = load_prompt("classify_and_describe_mask.txt")
    model_input.append({
        "type": "text",
        "text": prompt,
        })
    
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": model_input
            }
        ],
    )
    output = response.choices[0].message.content.replace("\n\n", "\n")
    if output.find('\n') == -1:
        return output.split('\\n')
    return output.split('\n')


def overlap(bbox1, bbox2):
    if bbox1[0] > bbox2[2] or bbox1[2] < bbox2[0]:
        return False
    if bbox1[1] > bbox2[3] or bbox1[3] < bbox2[1]:
        return False
    return True


def generate_rels(objs, frame_idx, image_path):
    client = get_client()
    rels = {}
    for i in range(len(objs)):
        for j in range(i+1, len(objs)):
            if overlap(objs[i].frames[frame_idx]['bbox'], objs[j].frames[frame_idx]['bbox']):
                model_input = []
                model_input.append({
                    "type": "image_url",
                    "image_url": {
                        "url":  f"data:image/jpeg;base64,{encode_image(image_path)}"
                    },
                    })
                
                prompt = load_prompt("generate_rels.txt")
                prompt = prompt.replace("{first_desc}", objs[i].desc)
                prompt = prompt.replace("{sec_desc}", objs[j].desc)
                model_input.append({
                    "type": "text",
                    "text": prompt,
                    })
                
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": model_input
                        }
                    ],
                )
                output = response.choices[0].message.content

                print('--------------------------------------')
                print(i, j)
                print(prompt)
                print('=> Output:', output)
                print('--------------------------------------')

                rel_keywords = ['ON', 'BESIDE', 'WITHIN', 'NOT TOUCHING']
                obj_pair = f'{i}, {j}'
                if output.lower().index('second') < output.lower().index('first'):
                    obj_pair = f'{j}, {i}'
                for rel_keyword in rel_keywords:
                    if output.find(rel_keyword) > -1 and rel_keyword != 'NOT TOUCHING':
                        rels[obj_pair] = rel_keyword
    return rels
