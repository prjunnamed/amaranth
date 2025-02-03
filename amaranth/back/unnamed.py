__all__ = ["convert"]

import typing
import re

from ..lib import wiring
from ..hdl import _ast, _ir, _nir


class UnnamedNet:
    def __init__(self, cell, bit):
        self.cell = cell
        self.bit = bit

    @staticmethod
    def from_const(value):
        assert value in (0, 1)
        return UnnamedNet(None, value)

    def __eq__(self, other):
        return type(self) == type(other) and self.cell == other.cell and self.bit == other.bit

    def __ne__(self, other):
        return not (self == other)


class Emitter:
    def __init__(self, netlist: _nir.Netlist):
        self.netlist = netlist
        self.lines = []
        self.net_map = {
            _nir.Net.from_const(0): UnnamedNet.from_const(0),
            _nir.Net.from_const(1): UnnamedNet.from_const(1),
        }
        self.cell_map = {}
        self.ucell_width = {}
        self.memory_ports = {}
        self.next_ucell = 0

    def escape_string(self, s: str):
        chars = re.sub(r'[^"\x20-\x7e]+', lambda m: re.sub(r'(..)', r'\\\1', m[0].encode().hex()), s)
        return f'"{chars}"'

    def reserve_ucell(self, width):
        result = self.next_ucell
        self.next_ucell += max(width, 1)
        self.ucell_width[result] = width
        return result

    def assign_nets(self, value, uvalue):
        value = _nir.Value(value)
        if isinstance(uvalue, UnnamedNet):
            uvalue = [uvalue]
        assert len(value) == len(uvalue)
        for net, unet in zip(value, uvalue):
            self.net_map[net] = unet

    def ucell_output(self, ucell):
        return [UnnamedNet(ucell, bit) for bit in range(self.ucell_width[ucell])]

    def uzero(self, width):
        return [UnnamedNet.from_const(0)] * width

    def uones(self, width):
        return [UnnamedNet.from_const(1)] * width

    def value(self, value):
        return [self.net_map[net] for net in _nir.Value(value)]

    def uvalue_str(self, uvalue: typing.List[UnnamedNet]):
        if not uvalue:
            return "[]"
        elif all(unet.cell is None for unet in uvalue):
            return "".join(str(unet.bit) for unet in uvalue[::-1])
        elif uvalue[0].cell is not None and uvalue == self.ucell_output(uvalue[0].cell):
            ucell = uvalue[0].cell
            if len(uvalue) == 1:
                return f"%{ucell}"
            else:
                return f"%{ucell}:{self.ucell_width[ucell]}"
        elif len(uvalue) == 1:
            return f"%{uvalue[0].cell}+{uvalue[0].bit}"
        else:
            bits = " ".join(
                f"{unet.bit}" if unet.cell is None else f"%{unet.cell}+{unet.bit}"
                for unet in uvalue[::-1]
            )
            return f"[ {bits} ]"

    def iovalue_str(self, value):
        value = _nir.IOValue(value)
        if not value:
            return "[]"
        port0 = self.netlist.io_ports[value[0].port]
        if len(port0) == len(value) and all(net.port == value[0].port and net.bit == bit for bit, net in enumerate(value)):
            if len(value) == 1:
                return f"&{self.escape_string(port0.name)}"
            else:
                return f"&{self.escape_string(port0.name)}:{len(value)}"
        elif len(value) == 1:
            return f"&{self.escape_string(port0.name)}+{value[0].bit}"
        else:
            bits = " ".join(
                f"&{self.escape_string(self.netlist.io_ports[net.port].name)}+{net.bit}"
                for net in value[::-1]
            )
            return f"[ {bits} ]"

    def emit_ucell_raw(self, ucell, text, has_width=True):
        assert type(ucell) is int
        if has_width:
            self.lines.append(f"%{ucell}:{self.ucell_width[ucell]} = {text}")
        else:
            self.lines.append(f"%{ucell}:_ = {text}")

    def emit_ucell(self, ucell, opcode, *args, has_width=True):
        args = [
            self.uvalue_str(self.value(arg)) if isinstance(arg, (_nir.Net, _nir.Value)) else
            self.iovalue_str(arg) if isinstance(arg, (_nir.IONet, _nir.IOValue)) else
            arg if type(arg) is str else
            f"#{arg}" if type(arg) is int else
            self.uvalue_str(arg)
            for arg in args
        ]
        self.emit_ucell_raw(ucell, " ".join([opcode, *args]), has_width=has_width)

    def emit(self):
        # emit IOs
        for port in self.netlist.io_ports:
            self.lines.append(f"&{self.escape_string(port.name)}:{len(port)}")

        # emit inputs
        for (name, (start, width)) in self.netlist.top.ports_i.items():
            value = _nir.Value(_nir.Net.from_cell(0, start + bit) for bit in range(width))
            ucell = self.reserve_ucell(width)
            self.assign_nets(value, self.ucell_output(ucell))
            self.emit_ucell(ucell, "input", self.escape_string(name))

        # collect memory ports
        for cell_index, cell in enumerate(self.netlist.cells):
            if isinstance(cell, (_nir.SyncWritePort, _nir.SyncReadPort, _nir.AsyncReadPort)):
                self.memory_ports.setdefault(cell.memory, []).append(cell_index)

        # reserve cell indices
        for cell_index, cell in enumerate(self.netlist.cells):
            if isinstance(cell, _nir.Top):
                pass
            elif isinstance(cell, _nir.Operator):
                value = _nir.Value(_nir.Net.from_cell(cell_index, bit) for bit in range(cell.width))
                if cell.operator == '~' and len(cell.inputs) == 1:
                    ucell = self.reserve_ucell(cell.width)
                    self.assign_nets(value, self.ucell_output(ucell))
                    self.cell_map[cell_index] = ucell
                elif cell.operator == '-' and len(cell.inputs) in (1, 2):
                    ucell_not = self.reserve_ucell(cell.width)
                    ucell_adc = self.reserve_ucell(cell.width + 1)
                    self.assign_nets(value, self.ucell_output(ucell_adc)[:cell.width])
                    self.cell_map[cell_index] = (ucell_not, ucell_adc)
                elif cell.operator in ('b', 'r|') and len(cell.inputs) == 1:
                    ucell_eq = self.reserve_ucell(1)
                    ucell_not = self.reserve_ucell(1)
                    self.assign_nets(value, self.ucell_output(ucell_not))
                    self.cell_map[cell_index] = (ucell_eq, ucell_not)
                elif cell.operator == 'r&' and len(cell.inputs) == 1:
                    ucell = self.reserve_ucell(1)
                    self.assign_nets(value, self.ucell_output(ucell))
                    self.cell_map[cell_index] = ucell
                elif cell.operator == 'r^' and len(cell.inputs) == 1:
                    ucells = [self.reserve_ucell(1) for _ in cell.inputs[0]]
                    if ucells:
                        self.assign_nets(value, self.ucell_output(ucells[-1]))
                    else:
                        self.assign_nets(value, self.uzero(1))
                    self.cell_map[cell_index] = ucells
                elif cell.operator == '+' and len(cell.inputs) == 2:
                    ucell = self.reserve_ucell(cell.width + 1)
                    self.assign_nets(value, self.ucell_output(ucell)[:cell.width])
                    self.cell_map[cell_index] = ucell
                elif cell.operator in ('*', '&', '^', '|', '<<', 'u>>', 's>>', '==', 'u<', 'u>', 's<', 's>') and len(cell.inputs) == 2:
                    ucell = self.reserve_ucell(cell.width)
                    self.assign_nets(value, self.ucell_output(ucell))
                    self.cell_map[cell_index] = ucell
                elif cell.operator in ('u//', 's//', 'u%', 's%') and len(cell.inputs) == 2:
                    ucell_eq = self.reserve_ucell(1)
                    ucell_div = self.reserve_ucell(cell.width)
                    ucell_mux = self.reserve_ucell(cell.width)
                    self.assign_nets(value, self.ucell_output(ucell_mux))
                    self.cell_map[cell_index] = (ucell_eq, ucell_div, ucell_mux)
                elif cell.operator in ('!=', 'u<=', 'u>=', 's<=', 's>=') and len(cell.inputs) == 2:
                    ucell_cmp = self.reserve_ucell(1)
                    ucell_not = self.reserve_ucell(1)
                    self.assign_nets(value, self.ucell_output(ucell_not))
                    self.cell_map[cell_index] = (ucell_cmp, ucell_not)
                elif cell.operator == 'm' and len(cell.inputs) == 3:
                    ucell = self.reserve_ucell(cell.width)
                    self.assign_nets(value, self.ucell_output(ucell))
                    self.cell_map[cell_index] = ucell
                else:
                    assert False # :nocov:
            elif isinstance(cell, _nir.Part):
                value = _nir.Value(_nir.Net.from_cell(cell_index, bit) for bit in range(cell.width))
                ucell = self.reserve_ucell(max(cell.width, len(cell.value)))
                self.assign_nets(value, self.ucell_output(ucell)[:len(value)])
                self.cell_map[cell_index] = ucell
            elif isinstance(cell, _nir.Match):
                value = _nir.Value(_nir.Net.from_cell(cell_index, bit) for bit in range(len(cell.patterns)))
                ucell = self.reserve_ucell(len(cell.patterns))
                self.assign_nets(value, self.ucell_output(ucell))
                self.cell_map[cell_index] = ucell
            elif isinstance(cell, _nir.AssignmentList):
                value = _nir.Value(_nir.Net.from_cell(cell_index, bit) for bit in range(len(cell.default)))
                ucells = [self.reserve_ucell(len(cell.default)) for _ in range(max(1, len(cell.assignments)))]
                if ucells:
                    self.assign_nets(value, self.ucell_output(ucells[-1]))
                self.cell_map[cell_index] = ucells
            elif isinstance(cell, _nir.FlipFlop):
                value = _nir.Value(_nir.Net.from_cell(cell_index, bit) for bit in range(len(cell.data)))
                ucell = self.reserve_ucell(len(cell.data))
                self.assign_nets(value, self.ucell_output(ucell))
                self.cell_map[cell_index] = ucell
            elif isinstance(cell, _nir.Memory):
                width = 0
                for port_index in self.memory_ports.setdefault(cell_index, []):
                    port = self.netlist.cells[port_index]
                    if isinstance(port, (_nir.SyncReadPort, _nir.AsyncReadPort)):
                        width += port.width
                ucell = self.reserve_ucell(width)
                offset = 0
                for port_index in self.memory_ports[cell_index]:
                    port = self.netlist.cells[port_index]
                    if isinstance(port, (_nir.SyncReadPort, _nir.AsyncReadPort)):
                        data = _nir.Value(_nir.Net.from_cell(port_index, bit) for bit in range(port.width))
                        self.assign_nets(data, self.ucell_output(ucell)[offset:offset + port.width])
                        offset += port.width
                self.cell_map[cell_index] = ucell
            elif isinstance(cell, (_nir.SyncWritePort, _nir.SyncReadPort, _nir.AsyncReadPort)):
                pass
            elif isinstance(cell, _nir.Instance):
                for start, width in cell.ports_o.values():
                    value = _nir.Value(_nir.Net.from_cell(cell_index, start + bit) for bit in range(width))
                    ucell = self.reserve_ucell(width)
                    self.assign_nets(value, self.ucell_output(ucell))
                    if start == 0:
                        self.cell_map[cell_index] = ucell
                if len(cell.ports_o) == 0:
                    self.cell_map[cell_index] = self.reserve_ucell(0)
            elif isinstance(cell, _nir.IOBuffer):
                value = _nir.Value(_nir.Net.from_cell(cell_index, bit) for bit in range(len(cell.port)))
                ucell = self.reserve_ucell(len(cell.o))
                self.assign_nets(value, self.ucell_output(ucell))
                self.cell_map[cell_index] = ucell
            else:
                raise TypeError(f"{cell.__class__.__name__} is not currently supported for Unnamed output")

        # emit cells
        for cell_index, cell in enumerate(self.netlist.cells):
            if isinstance(cell, _nir.Top):
                pass
            elif isinstance(cell, _nir.Operator):
                if cell.operator == '~' and len(cell.inputs) == 1:
                    ucell = self.cell_map[cell_index]
                    self.emit_ucell(ucell, "not", cell.inputs[0])
                elif cell.operator == '-' and len(cell.inputs) == 1:
                    (ucell_not, ucell_adc) = self.cell_map[cell_index]
                    self.emit_ucell(ucell_not, "not", cell.inputs[0])
                    self.emit_ucell(ucell_adc, "adc", self.ucell_output(ucell_not), self.uzero(cell.width), "1")
                elif cell.operator in ('b', 'r|') and len(cell.inputs) == 1:
                    (ucell_eq, ucell_not) = self.cell_map[cell_index]
                    self.emit_ucell(ucell_eq, "eq", cell.inputs[0], self.uzero(len(cell.inputs[0])))
                    self.emit_ucell(ucell_not, "not", self.ucell_output(ucell_eq))
                elif cell.operator == 'r&' and len(cell.inputs) == 1:
                    ucell = self.cell_map[cell_index]
                    self.emit_ucell(ucell, "eq", cell.inputs[0], self.uones(len(cell.inputs[0])))
                elif cell.operator == 'r^' and len(cell.inputs) == 1:
                    prev = self.uzero(1)
                    ucells = self.cell_map[cell_index]
                    for index, ucell in enumerate(ucells):
                        self.emit_ucell(ucell, "xor", prev, cell.inputs[0][index])
                        prev = self.ucell_output(ucell)
                elif cell.operator == '+' and len(cell.inputs) == 2:
                    ucell = self.cell_map[cell_index]
                    self.emit_ucell(ucell, "adc", cell.inputs[0], cell.inputs[1], "0")
                elif cell.operator == '-' and len(cell.inputs) == 2:
                    (ucell_not, ucell_adc) = self.cell_map[cell_index]
                    self.emit_ucell(ucell_not, "not", cell.inputs[1])
                    self.emit_ucell(ucell_adc, "adc", cell.inputs[0], self.ucell_output(ucell_not), "1")
                elif cell.operator in ('*', '&', '^', '|', '==', 'u<', 'u>', 's<', 's>') and len(cell.inputs) == 2:
                    opcode = {
                        '*': "mul",
                        '&': "and",
                        '|': "or",
                        '^': "xor",
                        '==': "eq",
                        'u<': "ult",
                        'u>': "ult",
                        's<': "slt",
                        's>': "slt",
                    }[cell.operator]
                    ucell = self.cell_map[cell_index]
                    if cell.operator in ('u>', 's>'):
                        self.emit_ucell(ucell, opcode, cell.inputs[1], cell.inputs[0])
                    else:
                        self.emit_ucell(ucell, opcode, cell.inputs[0], cell.inputs[1])
                elif cell.operator in ('u//', 's//', 'u%', 's%') and len(cell.inputs) == 2:
                    opcode = {
                        'u//': "udiv",
                        's//': "sdivfloor",
                        'u%': "umod",
                        's%': "smodfloor",
                    }[cell.operator]
                    (ucell_eq, ucell_div, ucell_mux) = self.cell_map[cell_index]
                    self.emit_ucell(ucell_eq, "eq", self.uzero(cell.width), cell.inputs[1])
                    self.emit_ucell(ucell_div, opcode, cell.inputs[0], cell.inputs[1])
                    self.emit_ucell(ucell_mux, "mux", self.ucell_output(ucell_eq), self.uzero(cell.width), self.ucell_output(ucell_div))
                elif cell.operator in ('<<', 'u>>', 's>>') and len(cell.inputs) == 2:
                    opcode = {
                        '<<': "shl",
                        'u>>': "ushr",
                        's>>': "sshr",
                    }[cell.operator]
                    ucell = self.cell_map[cell_index]
                    self.emit_ucell(ucell, opcode, cell.inputs[0], cell.inputs[1], 1)
                elif cell.operator in ('!=', 'u<=', 'u>=', 's<=', 's>=') and len(cell.inputs) == 2:
                    opcode = {
                        '!=': "eq",
                        'u>=': "ult",
                        'u<=': "ult",
                        's>=': "slt",
                        's<=': "slt",
                    }[cell.operator]
                    (ucell_cmp, ucell_not) = self.cell_map[cell_index]
                    if cell.operator in ('u<=', 's<='):
                        self.emit_ucell(ucell_cmp, opcode, cell.inputs[1], cell.inputs[0])
                    else:
                        self.emit_ucell(ucell_cmp, opcode, cell.inputs[0], cell.inputs[1])
                    self.emit_ucell(ucell_not, "not", self.ucell_output(ucell_cmp))
                elif cell.operator == 'm' and len(cell.inputs) == 3:
                    ucell = self.cell_map[cell_index]
                    self.emit_ucell(ucell, "mux", cell.inputs[0], cell.inputs[1], cell.inputs[2])
                else:
                    assert False # :nocov:
            elif isinstance(cell, _nir.Part):
                ucell = self.cell_map[cell_index]
                value = self.value(cell.value)
                if len(value) < cell.width:
                    if cell.value_signed:
                        value += [value[-1]] * (cell.width - len(value))
                    else:
                        value += self.uzero(cell.width - len(value))
                opcode = "sshr" if cell.value_signed else "ushr"
                self.emit_ucell(ucell, opcode, value, cell.offset, cell.stride)
            elif isinstance(cell, _nir.Match):
                ucell = self.cell_map[cell_index]
                en = "en=" + self.uvalue_str(self.value(cell.en))
                self.emit_ucell(ucell, "match", en, cell.value, "{")
                for alternates in cell.patterns:
                    alternates = [pattern.replace('-', 'X') for pattern in alternates]
                    if len(alternates) == 1:
                        self.lines.append("  " + alternates[0])
                    else:
                        self.lines.append("  (" + " ".join(alternates) + ")")
                self.lines.append("}")
            elif isinstance(cell, _nir.AssignmentList):
                ucells = self.cell_map[cell_index]
                if len(cell.assignments) == 0:
                    self.emit_ucell(ucells[0], "buf", cell.default)
                else:
                    prev = self.value(cell.default)
                    for assignment, ucell in zip(cell.assignments, ucells):
                        en = "en=" + self.uvalue_str(self.value(assignment.cond))
                        at = f"at=#{assignment.start}"
                        self.emit_ucell(ucell, "assign", en, prev, assignment.value, at)
                        prev = self.ucell_output(ucell)
            elif isinstance(cell, _nir.FlipFlop):
                ucell = self.cell_map[cell_index]
                if cell.clk_edge == "pos":
                    clk = "clk=" + self.uvalue_str(self.value(cell.clk))
                else:
                    clk = "clk=!" + self.uvalue_str(self.value(cell.clk))
                init = f"init={cell.init:0{len(cell.data)}b}"
                if cell.arst == _nir.Net.from_const(0):
                    self.emit_ucell(ucell, "dff", cell.data, clk, init)
                else:
                    arst = "arst=" + self.uvalue_str(self.value(cell.arst))
                    self.emit_ucell(ucell, "dff", cell.data, clk, arst, init)
            elif isinstance(cell, _nir.Memory):
                ucell = self.cell_map[cell_index]
                self.emit_ucell(ucell, "memory", f"depth=#{cell.depth}", f"width=#{cell.width}", "{")
                for init in cell.init:
                    self.lines.append(f"  init {init:0{cell.width}b}")
                write_port_indices = []
                for port_index in self.memory_ports[cell_index]:
                    port = self.netlist.cells[port_index]
                    if isinstance(port, _nir.SyncWritePort):
                        write_port_indices.append(port_index)
                for port_index in self.memory_ports[cell_index]:
                    port = self.netlist.cells[port_index]
                    addr = self.uvalue_str(self.value(port.addr))
                    if isinstance(port, _nir.SyncWritePort):
                        data = self.uvalue_str(self.value(port.data))
                        mask = self.uvalue_str(self.value(port.en))
                        if port.clk_edge == "neg":
                            clk = "!" + self.uvalue_str(self.value(port.clk))
                        else:
                            clk = self.uvalue_str(self.value(port.clk))
                        self.lines.append(f"  write addr={addr} data={data} mask={mask} clk={clk}")
                    elif isinstance(port, _nir.AsyncReadPort):
                        self.lines.append(f"  read addr={addr} width=#{port.width}")
                    elif isinstance(port, _nir.SyncReadPort):
                        if port.clk_edge == "neg":
                            clk = "!" + self.uvalue_str(self.value(port.clk))
                        else:
                            clk = self.uvalue_str(self.value(port.clk))
                        en = self.uvalue_str(self.value(port.en))
                        relations = []
                        for write_port_index in write_port_indices:
                            write_port = self.netlist.cells[write_port_index]
                            if write_port_index in port.transparent_for:
                                relations.append("trans")
                            elif write_port.clk == port.clk and write_port.clk_edge == port.clk_edge:
                                relations.append("rdfirst")
                            else:
                                relations.append("undef")
                        relations = " ".join(relations)
                        self.lines.append(f"  read addr={addr} width=#{port.width} clk={clk} en={en} [{relations}]")
                    else:
                        assert False # :nocov:
                self.lines.append("}")
            elif isinstance(cell, (_nir.SyncWritePort, _nir.SyncReadPort, _nir.AsyncReadPort)):
                pass
            elif isinstance(cell, _nir.Instance):
                ucell = self.cell_map[cell_index]
                self.emit_ucell(ucell, self.escape_string(cell.type), "{", has_width=False)
                for name, value in cell.parameters.items():
                    if type(value) is int:
                        self.lines.append(f"  param {self.escape_string(name)} = #{value}")
                    elif type(value) is _ast.Const:
                        value = f"{value.value:0{len(value)}b}"
                        self.lines.append(f"  param {self.escape_string(name)} = {value}")
                    elif type(value) is str:
                        self.lines.append(f"  param {self.escape_string(name)} = {self.escape_string(value)}")
                    else:
                        raise TypeError("cannot handle parameter {value!r}")
                for name, value in cell.ports_i.items():
                    self.lines.append(f"  input {self.escape_string(name)}={self.uvalue_str(self.value(value))}")
                for name, (start, width) in cell.ports_o.items():
                    self.lines.append(f"  %{ucell + start}:{width} = output {self.escape_string(name)}")
                for name, (value, _dir) in cell.ports_io.items():
                    self.lines.append(f"  io {self.escape_string(name)} = {self.iovalue_str(value)}")
                self.lines.append("}")
            elif isinstance(cell, _nir.IOBuffer):
                ucell = self.cell_map[cell_index]
                o = "o=" + self.uvalue_str(self.value(cell.o))
                en = "en=" + self.uvalue_str(self.value(cell.oe))
                self.emit_ucell(ucell, "iobuf", cell.port, o, en)
            else:
                assert False # :nocov:

        for (name, value) in self.netlist.top.ports_o.items():
            ucell = self.reserve_ucell(0)
            self.emit_ucell(ucell, "output", self.escape_string(name), value)

        for module in self.netlist.modules:
            for signal, name in module.signal_names.items():
                value = self.netlist.signals[signal]
                hier_name = " ".join(module.name + (name,))
                ucell = self.reserve_ucell(0)
                self.emit_ucell(ucell, "name", self.escape_string(hier_name), value)

        return "\n".join(self.lines) + "\n"


def convert_fragment(fragment, ports=(), name="top", *, emit_src=True, **kwargs):
    assert isinstance(fragment, (_ir.Fragment, _ir.Design))
    netlist = _ir.build_netlist(fragment, ports=ports, name=name, **kwargs)
    return Emitter(netlist).emit()


def convert(elaboratable, name="top", platform=None, *, ports=None, **kwargs):
    if (ports is None and
            hasattr(elaboratable, "signature") and
            isinstance(elaboratable.signature, wiring.Signature)):
        ports = {}
        for path, member, value in elaboratable.signature.flatten(elaboratable):
            if isinstance(value, _ast.ValueCastable):
                value = value.as_value()
            if isinstance(value, _ast.Value):
                if member.flow == wiring.In:
                    dir = _ir.PortDirection.Input
                else:
                    dir = _ir.PortDirection.Output
                ports["__".join(map(str, path))] = (value, dir)
    elif ports is None:
        raise TypeError("The `convert()` function requires a `ports=` argument")
    fragment = _ir.Fragment.get(elaboratable, platform)
    return convert_fragment(fragment, ports, name, **kwargs)
