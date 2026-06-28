from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SceneGraphNode:
    object_id: str
    name: str
    category: str
    visible: bool
    position: Optional[List[float]] = None
    orientation: Optional[List[float]] = None
    states: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "name": self.name,
            "category": self.category,
            "visible": self.visible,
            "position": self.position,
            "orientation": self.orientation,
            "states": self.states,
        }


@dataclass
class SceneGraphEdge:
    source_id: str
    target_id: str
    relation: str
    source: str = "omnigibson"
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class SceneGraphSnapshot:
    step_index: int
    primitive_name: Optional[str]
    raw_plan: Optional[str]
    nodes: List[SceneGraphNode] = field(default_factory=list)
    edges: List[SceneGraphEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "primitive_name": self.primitive_name,
            "raw_plan": self.raw_plan,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "metadata": self.metadata,
        }

    def to_prompt_context(self) -> str:
        '''
            它主要给 planner / LLM 使用：
            scene graph 本身是结构化对象，但 LLM prompt 更适合读自然语言/文本，
            所以这个函数就是把 scene graph 快速压缩成 prompt context。

            - apple: visible, Inside=False, Open=False
            - cabinet: visible, Open=True
            - apple in cabinet
        '''
    
        lines = []
        for node in self.nodes:
            state_text = ", ".join(
                f"{key}={value}" for key, value in sorted(node.states.items())
            )
            visible_text = "visible" if node.visible else "not_visible"
            if state_text:
                lines.append(f"- {node.name}: {visible_text}, {state_text}")
            else:
                lines.append(f"- {node.name}: {visible_text}")

        for edge in self.edges:
            lines.append(f"- {edge.source_id} {edge.relation} {edge.target_id}")

        return "\n".join(lines)
