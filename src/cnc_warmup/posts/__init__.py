"""Post-processors: one per controller, each rendering a WarmupPlan as an NC program."""

from collections.abc import Callable, Mapping

from cnc_warmup.model import Controller
from cnc_warmup.plan import WarmupPlan
from cnc_warmup.posts import fanuc, heidenhain
from cnc_warmup.posts.base import Program

POSTS: Mapping[Controller, Callable[[WarmupPlan], Program]] = {
    Controller.HEIDENHAIN: heidenhain.render,
    Controller.FANUC: fanuc.render,
}


def render(plan: WarmupPlan, controller: Controller) -> Program:
    return POSTS[controller](plan)
