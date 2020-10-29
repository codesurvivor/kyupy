import numpy as np
import math
from . import numba


TMAX = np.float32(2 ** 127)  # almost np.PINF for 32-bit floating point values
TMIN = np.float32(-2 ** 127)  # almost np.NINF for 32-bit floating point values


class WaveSim:
    def __init__(self, circuit, timing, sims=8, wavecaps=16):
        self.circuit = circuit
        self.sims = sims
        self.overflows = 0
        self.interface = list(circuit.interface) + [n for n in circuit.nodes if 'dff' in n.kind.lower()]

        self.lst_eat_valid = False

        self.cdata = np.zeros((len(self.interface), sims, 6), dtype='float32')

        if type(wavecaps) is int:
            wavecaps = [wavecaps] * len(circuit.lines)

        intf_wavecap = 4  # sufficient for storing only 1 transition.

        # state allocation table. maps line and interface indices to self.state memory locations

        self.sat = np.zeros((len(circuit.lines) + 2 + 2 * len(self.interface), 2), dtype='int')
        self.sat[:, 0] = -1
        filled = 0
        for lidx, cap in enumerate(wavecaps):
            self.sat[lidx] = filled, cap
            filled += cap

        self.zero_idx = len(circuit.lines)
        self.sat[self.zero_idx] = filled, intf_wavecap
        filled += intf_wavecap
        self.tmp_idx = self.zero_idx + 1
        self.sat[self.tmp_idx] = filled, intf_wavecap
        filled += intf_wavecap

        self.ppi_offset = self.tmp_idx + 1
        self.ppo_offset = self.ppi_offset + len(self.interface)
        for i, n in enumerate(self.interface):
            if len(n.outs) > 0:
                self.sat[self.ppi_offset + i] = filled, intf_wavecap
                filled += intf_wavecap
            if len(n.ins) > 0:
                self.sat[self.ppo_offset + i] = self.sat[n.ins[0].index]

        # pad timing
        self.timing = np.zeros((len(self.sat), 2, 2))
        self.timing[:len(timing)] = timing

        # allocate self.state
        self.state = np.zeros((filled, sims), dtype='float32') + TMAX

        # generate self.ops
        ops = []
        interface_dict = dict([(n, i) for i, n in enumerate(self.interface)])
        for n in circuit.topological_order():
            if n in interface_dict:
                inp_idx = self.ppi_offset + interface_dict[n]
                if len(n.outs) > 0 and n.outs[0] is not None:
                    ops.append((0b1010, n.outs[0].index, inp_idx, self.zero_idx))
                if 'dff' in n.kind.lower():
                    if len(n.outs) > 1 and n.outs[1] is not None:
                        ops.append((0b0101, n.outs[1].index, inp_idx, self.zero_idx))
                else:
                    for o_line in n.outs[1:]:
                        if o_line is not None:
                            ops.append((0b1010, o_line.index, inp_idx, self.zero_idx))
            else:
                o0_idx = self.tmp_idx
                i0_idx = self.zero_idx
                i1_idx = self.zero_idx
                if len(n.outs) > 0 and n.outs[0] is not None:
                    o0_idx = n.outs[0].index
                else:
                    print(f'no outputs for {n}')
                if len(n.ins) > 0 and n.ins[0] is not None: i0_idx = n.ins[0].index
                if len(n.ins) > 1 and n.ins[1] is not None: i1_idx = n.ins[1].index
                kind = n.kind.lower()
                if kind == '__fork__':
                    for o_line in n.outs:
                        ops.append((0b1010, o_line.index, i0_idx, i1_idx))
                elif kind.startswith('nand'):
                    ops.append((0b0111, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('nor'):
                    ops.append((0b0001, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('and'):
                    ops.append((0b1000, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('or'):
                    ops.append((0b1110, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('xor'):
                    ops.append((0b0110, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('xnor'):
                    ops.append((0b1001, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('not') or kind.startswith('inv'):
                    ops.append((0b0101, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('buf') or kind.startswith('nbuf'):
                    ops.append((0b1010, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('__const1__') or kind.startswith('tieh'):
                    ops.append((0b0101, o0_idx, i0_idx, i1_idx))
                elif kind.startswith('__const0__') or kind.startswith('tiel'):
                    ops.append((0b1010, o0_idx, i0_idx, i1_idx))
                else:
                    print('unknown gate type', kind)
        self.ops = np.asarray(ops, dtype='int32')

        # generate level data
        levels = np.zeros(len(self.sat), dtype='int32')
        level_starts = [0]
        current_level = 1
        for i, op in enumerate(self.ops):
            if levels[op[2]] >= current_level or levels[op[3]] >= current_level:
                current_level += 1
                level_starts.append(i)
            levels[op[1]] = current_level
        self.level_starts = np.asarray(level_starts, dtype='int32')
        self.level_stops = np.asarray(level_starts[1:] + [len(self.ops)], dtype='int32')

        m1 = np.array([2 ** x for x in range(7, -1, -1)], dtype='uint8')
        m0 = ~m1
        self.mask = np.rollaxis(np.vstack((m0, m1)), 1)

    def get_line_delay(self, line, polarity):
        return self.timing[line, 0, polarity]

    def set_line_delay(self, line, polarity, delay):
        self.timing[line, 0, polarity] = delay

    def assign(self, vectors, time=0.0, offset=0):
        nvectors = min(vectors.nvectors - offset, self.sims)
        for i, node in enumerate(self.interface):
            ppi_loc = self.sat[self.ppi_offset + i, 0]
            if ppi_loc < 0: continue
            for p in range(nvectors):
                vector = p + offset
                a = vectors.bits[i, :, vector // 8]
                m = self.mask[vector % 8]
                toggle = 0
                if a[0] & m[1]:
                    self.state[ppi_loc, p] = TMIN
                    toggle += 1
                if (len(a) > 2) and (a[2] & m[1]) and ((a[0] & m[1]) == (a[1] & m[1])):
                    self.state[ppi_loc + toggle, p] = time
                    toggle += 1
                self.state[ppi_loc + toggle, p] = TMAX

    def propagate(self, sims=None, sd=0.0, seed=1):
        if sims is None:
            sims = self.sims
        else:
            sims = min(sims, self.sims)
        for op_start, op_stop in zip(self.level_starts, self.level_stops):
            self.overflows += level_eval(self.ops, op_start, op_stop, self.state, self.sat, 0, sims,
                                         self.timing, sd, seed)
        self.lst_eat_valid = False

    def wave(self, line, vector):
        if line < 0:
            return [TMAX]
        mem, wcap = self.sat[line]
        if mem < 0:
            return [TMAX]
        return self.state[mem:mem + wcap, vector]

    def wave_ppi(self, i, vector):
        return self.wave(self.ppi_offset + i, vector)

    def wave_ppo(self, o, vector):
        return self.wave(self.ppo_offset + o, vector)

    def capture(self, time=TMAX, sd=0, seed=1, probabilities=None, offset=0):
        for i, node in enumerate(self.interface):
            if len(node.ins) == 0: continue
            for p in range(self.sims):
                self.cdata[i, p] = self.capture_wave(self.ppo_offset + i, p, time, sd, seed)
        if probabilities is not None:
            assert offset < probabilities.shape[1]
            cap_dim = min(probabilities.shape[1] - offset, self.sims)
            probabilities[:, offset:cap_dim + offset] = self.cdata[:, 0:cap_dim, 0]
        self.lst_eat_valid = True
        return self.cdata

    def reassign(self, time=0.0):
        for i, node in enumerate(self.interface):
            ppi_loc = self.sat[self.ppi_offset + i]
            ppo_loc = self.sat[self.ppo_offset + i]
            if ppi_loc < 0 or ppo_loc < 0: continue
            for sidx in range(self.sims):
                ival = self.val(self.ppi_offset + i, sidx, TMAX) > 0.5
                oval = self.cdata[i, sidx, 1] > 0.5
                toggle = 0
                if ival:
                    self.state[ppi_loc, sidx] = TMIN
                    toggle += 1
                if ival != oval:
                    self.state[ppi_loc + toggle, sidx] = time
                    toggle += 1
                self.state[ppi_loc + toggle, sidx] = TMAX

    def eat(self, line, vector):
        eat = TMAX
        for t in self.wave(line, vector):
            if t >= TMAX: break
            if t <= TMIN: continue
            eat = min(eat, t)
        return eat

    def lst(self, line, vector):
        lst = TMIN
        for t in self.wave(line, vector):
            if t >= TMAX: break
            if t <= TMIN: continue
            lst = max(lst, t)
        return lst

    def lst_ppo(self, o, vector):
        if not self.lst_eat_valid:
            self.capture()
        return self.cdata[o, vector, 5]

    def toggles(self, line, vector):
        tog = 0
        for t in self.wave(line, vector):
            if t >= TMAX: break
            if t <= TMIN: continue
            tog += 1
        return tog

    def _vals(self, idx, vector, times, sd=0.0):
        s_sqrt2 = sd * math.sqrt(2)
        m = 0.5
        accs = [0.0] * len(times)
        values = [0] * len(times)
        for t in self.wave(idx, vector):
            if t >= TMAX: break
            for idx, time in enumerate(times):
                if t < time:
                    values[idx] = values[idx] ^ 1
            m = -m
            if t <= TMIN: continue
            if s_sqrt2 > 0:
                for idx, time in enumerate(times):
                    accs[idx] += m * (1 + math.erf((t - time) / s_sqrt2))
        if (m < 0) and (s_sqrt2 > 0):
            for idx, time in enumerate(times):
                accs[idx] += 1
        if s_sqrt2 == 0:
            return values
        else:
            return accs

    def vals(self, line, vector, times, sd=0):
        return self._vals(line, vector, times, sd)

    def val(self, line, vector, time=TMAX, sd=0):
        return self.capture_wave(line, vector, time, sd)[0]

    def vals_ppo(self, o, vector, times, sd=0):
        return self._vals(self.ppo_offset + o, vector, times, sd)

    def val_ppo(self, o, vector, time=TMAX, sd=0):
        if not self.lst_eat_valid:
            self.capture(time, sd)
        return self.cdata[o, vector, 0]

    def capture_wave(self, line, vector, time=TMAX, sd=0.0, seed=1):
        s_sqrt2 = sd * math.sqrt(2)
        m = 0.5
        acc = 0.0
        eat = TMAX
        lst = TMIN
        tog = 0
        val = int(0)
        final = int(0)
        for t in self.wave(line, vector):
            if t >= TMAX: break
            m = -m
            final ^= 1
            if t < time:
                val ^= 1
            if t <= TMIN: continue
            if s_sqrt2 > 0:
                acc += m * (1 + math.erf((t - time) / s_sqrt2))
            eat = min(eat, t)
            lst = max(lst, t)
            tog += 1
        if s_sqrt2 > 0:
            if m < 0:
                acc += 1
            if acc >= 0.99:
                val = 1
            elif acc > 0.01:
                seed = (seed << 4) + (vector << 20) + (line-self.ppo_offset << 1)
                seed = int(0xDEECE66D) * seed + 0xB
                seed = int(0xDEECE66D) * seed + 0xB
                rnd = float((seed >> 8) & 0xffffff) / float(1 << 24)
                val = rnd < acc
            else:
                val = 0
        else:
            acc = val

        return acc, val, final, (val != final), eat, lst


@numba.njit
def level_eval(ops, op_start, op_stop, state, sat, st_start, st_stop, line_times, sd, seed):
    overflows = 0
    for op_idx in range(op_start, op_stop):
        op = ops[op_idx]
        for st_idx in range(st_start, st_stop):
            overflows += wave_eval(op, state, sat, st_idx, line_times, sd, seed)
    return overflows


@numba.njit
def rand_gauss(seed, sd):
    clamp = 0.5
    if sd <= 0.0:
        return 1.0
    while True:
        x = -6.0
        for i in range(12):
            seed = int(0xDEECE66D) * seed + 0xB
            x += float((seed >> 8) & 0xffffff) / float(1 << 24)
        x *= sd
        if abs(x) <= clamp:
            break
    return x + 1.0


@numba.njit
def wave_eval(op, state, sat, st_idx, line_times, sd=0.0, seed=0):
    lut, z_idx, a_idx, b_idx = op
    overflows = int(0)

    _seed = (seed << 4) + (z_idx << 20) + (st_idx << 1)

    a_mem = sat[a_idx, 0]
    b_mem = sat[b_idx, 0]
    z_mem, z_cap = sat[z_idx]

    a_cur = int(0)
    b_cur = int(0)
    z_cur = lut & 1
    if z_cur == 1:
        state[z_mem, st_idx] = TMIN

    a = state[a_mem, st_idx] + line_times[a_idx, 0, z_cur] * rand_gauss(_seed ^ a_mem ^ z_cur, sd)
    b = state[b_mem, st_idx] + line_times[b_idx, 0, z_cur] * rand_gauss(_seed ^ b_mem ^ z_cur, sd)

    previous_t = TMIN

    current_t = min(a, b)
    inputs = int(0)

    while current_t < TMAX:
        z_val = z_cur & 1
        if b < a:
            b_cur += 1
            b = state[b_mem + b_cur, st_idx]
            b += line_times[b_idx, 0, z_val ^ 1] * rand_gauss(_seed ^ b_mem ^ z_val ^ 1, sd)
            thresh = line_times[b_idx, 1, z_val] * rand_gauss(_seed ^ b_mem ^ z_val, sd)
            inputs ^= 2
            next_t = b
        else:
            a_cur += 1
            a = state[a_mem + a_cur, st_idx]
            a += line_times[a_idx, 0, z_val ^ 1] * rand_gauss(_seed ^ a_mem ^ z_val ^ 1, sd)
            thresh = line_times[a_idx, 1, z_val] * rand_gauss(_seed ^ a_mem ^ z_val, sd)
            inputs ^= 1
            next_t = a

        if (z_cur & 1) != ((lut >> inputs) & 1):
            # we generate a toggle in z_mem, if:
            #   ( it is the first toggle in z_mem OR
            #   following toggle is earlier OR
            #   pulse is wide enough ) AND enough space in z_mem.
            if z_cur == 0 or next_t < current_t or (current_t - previous_t) > thresh:
                if z_cur < (z_cap - 1):
                    state[z_mem + z_cur, st_idx] = current_t
                    previous_t = current_t
                    z_cur += 1
                else:
                    overflows += 1
                    previous_t = state[z_mem + z_cur - 1, st_idx]
                    z_cur -= 1
            else:
                z_cur -= 1
                if z_cur > 0:
                    previous_t = state[z_mem + z_cur - 1, st_idx]
                else:
                    previous_t = TMIN
        current_t = min(a, b)

    state[z_mem + z_cur, st_idx] = TMAX
    return overflows