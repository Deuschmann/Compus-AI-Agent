"""Teacher-side modules for the biostatistics teaching agent."""

from teacher_agent.exam_module import ExamRequest, generate_exam
from teacher_agent.ppt_module import PPTRequest, generate_ppt

__all__ = [
    "ExamRequest",
    "PPTRequest",
    "generate_exam",
    "generate_ppt",
]
