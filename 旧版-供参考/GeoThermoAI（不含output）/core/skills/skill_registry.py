import importlib
from pathlib import Path
from typing import Dict, List, Optional
from .base_skill import BaseSkill

class SkillRegistry:
    """Skill注册中心 - 管理所有Skill的注册、查找和动态加载"""
    
    def __init__(self):
        self._skills: Dict[str, BaseSkill] = {}
        self._groups: Dict[str, List[str]] = {}
    
    def register(self, skill: BaseSkill):
        """注册一个Skill实例"""
        if skill.name in self._skills:
            raise ValueError(f"Skill '{skill.name}' 已注册")
        self._skills[skill.name] = skill
        self._groups.setdefault(skill.group, []).append(skill.name)
    
    def get(self, name: str) -> Optional[BaseSkill]:
        """按名称获取Skill"""
        return self._skills.get(name)
    
    def get_group(self, group: str) -> List[BaseSkill]:
        """获取同组的所有Skill（可互相替换）"""
        return [self._skills[n] for n in self._groups.get(group, []) if n in self._skills]
    
    def get_by_group(self, group: str, preferred: str = None) -> Optional[BaseSkill]:
        """从指定组获取一个Skill。若指定preferred名称则优先返回，否则返回第一个"""
        skills = self.get_group(group)
        if not skills:
            return None
        if preferred:
            for s in skills:
                if s.name == preferred:
                    return s
        return skills[0]
    
    def list_skills(self) -> List[Dict]:
        """列出所有已注册Skill的信息（供Agent使用）"""
        return [
            {
                "name": s.name, "group": s.group, "description": s.description,
                "parameters": [{"name": p.name, "type": p.type, 
                                "description": p.description, "required": p.required}
                               for p in s.parameters],
                "input_schema": s.input_schema,
                "output_schema": s.output_schema,
            }
            for s in self._skills.values()
        ]
    
    def load_third_party_skills(self, skills_dir: str = "skills"):
        """动态加载第三方Skill包"""
        skills_path = Path(skills_dir)
        if not skills_path.exists():
            return
        for skill_dir in skills_path.iterdir():
            if skill_dir.is_dir() and (skill_dir / "__init__.py").exists():
                try:
                    module = importlib.import_module(f"skills.{skill_dir.name}")
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (isinstance(attr, type) and 
                            issubclass(attr, BaseSkill) and attr != BaseSkill):
                            self.register(attr())
                except Exception as e:
                    print(f"加载第三方Skill失败 [{skill_dir.name}]: {e}")
    
    def get_tool_descriptions_for_llm(self) -> str:
        """生成供LLM Function Calling使用的工具描述文本"""
        lines = ["以下是你可以调用的技能（Skills）：\n"]
        for group, names in self._groups.items():
            lines.append(f"## [{group}] 组")
            for name in names:
                s = self._skills[name]
                lines.append(f"### {s.name}")
                lines.append(f"描述: {s.description}")
                lines.append("参数:")
                for p in s.parameters:
                    req = "必填" if p.required else "可选"
                    lines.append(f"  - {p.name} ({p.type}, {req}): {p.description}")
                lines.append("")
        return "\n".join(lines)
