from abc import ABC, abstractmethod
from typing import Any, Dict, List
from dataclasses import dataclass, field

@dataclass
class SkillParameter:
    """Skill参数描述 - 供LLM理解该Skill接受什么参数"""
    name: str               # 参数名，如 "n_estimators"
    type: str               # 类型: "string", "number", "boolean", "file_path"
    description: str        # 自然语言描述
    required: bool = True   # 是否必填
    default: Any = None     # 默认值
    choices: List = field(default_factory=list)  # 可选值列表

@dataclass
class Hyperparameter:
    """超参数描述 - 供UI动态渲染参数表单"""
    name: str               # 参数名
    label: str              # UI显示标签
    type: str               # "number" | "select" | "boolean"
    default: Any            # 默认值
    min: Any = None         # 数值型最小值
    max: Any = None         # 数值型最大值
    step: Any = None        # 数值型步长
    options: List = field(default_factory=list)  # type="select"时的选项列表
    description: str = ""   # 参数说明（tooltip）

@dataclass
class SkillResult:
    """Skill执行结果 - 同组Skill必须返回相同格式的data字典"""
    success: bool           # 是否成功
    message: str            # 人类可读的结果描述
    data: Dict[str, Any] = field(default_factory=dict)       # 结构化输出数据
    artifacts: List[str] = field(default_factory=list)       # 生成的文件路径列表

class BaseSkill(ABC):
    """所有Skill的基类"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Skill唯一标识"""
        pass
    
    @property
    @abstractmethod
    def group(self) -> str:
        """Skill分组标识，同组Skill可以互相替换"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """自然语言描述（1-2句话），供LLM理解该Skill的功能"""
        pass
    
    @property
    @abstractmethod
    def parameters(self) -> List[SkillParameter]:
        """该Skill接受的参数列表"""
        pass
    
    @property
    def hyperparameters(self) -> List[Hyperparameter]:
        """该Skill的超参数列表 - 供UI动态渲染参数表单"""
        return []
    
    @property
    @abstractmethod
    def input_schema(self) -> Dict[str, str]:
        """输入数据Schema，同组的所有Skill必须定义完全相同的input_schema"""
        pass
    
    @property
    @abstractmethod
    def output_schema(self) -> Dict[str, str]:
        """输出数据Schema，同组的所有Skill必须定义完全相同的output_schema"""
        pass
    
    @abstractmethod
    def execute(self, params: Dict[str, Any], 
                progress_callback=None, log_callback=None) -> SkillResult:
        """执行Skill的核心逻辑"""
        pass
