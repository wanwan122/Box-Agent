from pathlib import Path

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import build_auto_loaded_skills_prompt, turn_preload_skill_names


def test_explicit_cua_skill_is_discoverable_and_loads_from_builtin_manifest():
    root = Path(__file__).resolve().parents[1] / 'box_agent' / 'skills'
    loader = SkillLoader(sources=[(root, 'builtin')])
    loader.discover_skills()
    names = turn_preload_skill_names((), None, '通过 Driver 打开 Word 文档',
                                     selected_skill_names=('mac-cua',))
    loaded = build_auto_loaded_skills_prompt(loader, 'base system', names)
    assert loaded.loaded_names == ('mac-cua',)
    assert (root / 'mac-cua' / 'SKILL.md').read_text().split('---', 2)[2].strip() in loaded.system_prompt
