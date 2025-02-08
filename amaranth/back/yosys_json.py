from .._toolchain.yosys import *
from . import rtlil


__all__ = ["YosysError", "convert", "convert_fragment"]


def _convert_rtlil_text(rtlil_text, *, src_loc_at=0):
    yosys = find_yosys(lambda ver: ver >= (0, 10))

    script = []
    script.append(f"read_rtlil <<rtlil\n{rtlil_text}\nrtlil")
    script.append("proc -norom -noopt")
    script.append("memory_collect")
    script.append("write_json")

    return yosys.run(["-q", "-"], "\n".join(script), src_loc_at=1 + src_loc_at)


def convert_fragment(*args, **kwargs):
    rtlil_text, name_map = rtlil.convert_fragment(*args, **kwargs)
    return _convert_rtlil_text(rtlil_text, src_loc_at=1), name_map


def convert(*args, **kwargs):
    rtlil_text = rtlil.convert(*args, **kwargs)
    return _convert_rtlil_text(rtlil_text, src_loc_at=1)
