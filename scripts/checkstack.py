#!/usr/bin/env python
# Script that tries to find how much stack space each function in an
# object is using.
#
# Copyright (C) 2015  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Usage:
#   avr-objdump -d out/klipper.elf | scripts/checkstack.py

import sys
import re

# Functions that change stacks
STACKHOP = []
# List of functions we can assume are never called.
IGNORE = []

OUTPUTDESC = """
#funcname1[preamble_stack_usage,max_usage_with_callers]:
#    insn_addr:called_function [usage_at_call_point+caller_preamble,total_usage]
#
#funcname2[p,m,max_usage_to_yield_point]:
#    insn_addr:called_function [u+c,t,usage_to_yield_point]
"""

class function:
    def __init__(self, funcaddr, funcname):
        self.funcaddr = funcaddr
        self.funcname = funcname
        self.basic_stack_usage = 0
        self.max_stack_usage = None
        self.yield_usage = -1
        self.max_yield_usage = None
        self.total_calls = 0
        # called_funcs = [(insnaddr, calladdr, stackusage), ...]
        self.called_funcs = []
        self.subfuncs = {}
    # Update function info with a found "yield" point.
    def noteYield(self, stackusage):
        if self.yield_usage < stackusage:
            self.yield_usage = stackusage
    # Update function info with a found "call" point.
    def noteCall(self, insnaddr, calladdr, stackusage):
        if (calladdr, stackusage) in self.subfuncs:
            # Already noted a nearly identical call - ignore this one.
            return
        self.called_funcs.append((insnaddr, calladdr, stackusage))
        self.subfuncs[(calladdr, stackusage)] = 1

# Find out maximum stack usage for a function
def calcmaxstack(info, funcs):
    if info.max_stack_usage is not None:
        return
    info.max_stack_usage = max_stack_usage = info.basic_stack_usage
    info.max_yield_usage = max_yield_usage = info.yield_usage
    total_calls = 0
    seenbefore = {}
    # Find max of all nested calls.
    for insnaddr, calladdr, usage in info.called_funcs:
        callinfo = funcs.get(calladdr)
        if callinfo is None:
            continue
        calcmaxstack(callinfo, funcs)
        if callinfo.funcname not in seenbefore:
            seenbefore[callinfo.funcname] = 1
            total_calls += callinfo.total_calls + 1
        funcnameroot = callinfo.funcname.split('.')[0]
        if funcnameroot in IGNORE:
            # This called function is ignored - don't contribute it to
            # the max stack.
            continue
        totusage = usage + callinfo.max_stack_usage
        totyieldusage = usage + callinfo.max_yield_usage
        if funcnameroot in STACKHOP:
            # Don't count children of this function
            totusage = totyieldusage = usage
        if totusage > max_stack_usage:
            max_stack_usage = totusage
        if callinfo.max_yield_usage >= 0 and totyieldusage > max_yield_usage:
            max_yield_usage = totyieldusage
    info.max_stack_usage = max_stack_usage
    info.max_yield_usage = max_yield_usage
    info.total_calls = total_calls

# Try to arrange output so that functions that call each other are
# near each other.
def orderfuncs(funcaddrs, availfuncs):
    l = [(availfuncs[funcaddr].total_calls
          , availfuncs[funcaddr].funcname, funcaddr)
         for funcaddr in funcaddrs if funcaddr in availfuncs]
    l.sort()
    l.reverse()
    out = []
    while l:
        count, name, funcaddr = l.pop(0)
        info = availfuncs.get(funcaddr)
        if info is None:
            continue
        calladdrs = [calls[1] for calls in info.called_funcs]
        del availfuncs[funcaddr]
        out = out + orderfuncs(calladdrs, availfuncs) + [info]
    return out

hex_s = r'[0-9a-f]+'
re_func = re.compile(r'^(?P<funcaddr>' + hex_s + r') <(?P<func>.*)>:$')
re_asm = re.compile(
    r'^[ ]*(?P<insnaddr>' + hex_s
    + r'):\t[^\t]*\t(?P<insn>[^\t]+?)(?P<params>\t[^;]*)?'
    + r'[ ]*(; (?P<calladdr>0x' + hex_s
    + r') <(?P<ref>.*)>)?$')

def main():
    unknownfunc = function(None, "<unknown>")
    indirectfunc = function(-1, '<indirect>')
    unknownfunc.max_stack_usage = indirectfunc.max_stack_usage = 0
    unknownfunc.max_yield_usage = indirectfunc.max_yield_usage = -1
    funcs = {-1: indirectfunc}
    funcaddr = None
    datalines = {}
    cur = None
    atstart = 0
    stackusage = 0

    # Parse input lines
    for line in sys.stdin.readlines():
        m = re_func.match(line)
        if m is not None:
            # Found function
            funcaddr = int(m.group('funcaddr'), 16)
            funcs[funcaddr] = cur = function(funcaddr, m.group('func'))
            stackusage = 0
            atstart = 1
            continue
        m = re_asm.match(line)
        if m is None:
            if funcaddr not in datalines:
                datalines[funcaddr] = line.split()
            #print("other", repr(line))
            continue
        insn = m.group('insn')

        if insn == 'push':
            stackusage += 1
            continue
        if insn == 'rcall' and m.group('params').strip() == '.+0':
            stackusage += 2
            continue

        if atstart:
            if insn in ['in', 'eor']:
                continue
            cur.basic_stack_usage = stackusage
            atstart = 0

        insnaddr = m.group('insnaddr')
        calladdr = m.group('calladdr')
        if calladdr is None:
            if insn == 'ijmp':
                # Indirect tail call
                cur.noteCall(insnaddr, -1, 0)
            elif insn == 'icall':
                cur.noteCall(insnaddr, -1, stackusage + 2)
            else:
                # misc instruction
                continue
        else:
            # Jump or call insn
            calladdr = int(calladdr, 16)
            ref = m.group('ref')
            if '+' in ref:
                # Inter-function jump.
                pass
            elif insn in ('rjmp', 'jmp', 'brne', 'brcs'):
                # Tail call
                cur.noteCall(insnaddr, calladdr, 0)
            elif insn in ('rcall', 'call'):
                cur.noteCall(insnaddr, calladdr, stackusage + 2)
            else:
                print("unknown call", ref)
                cur.noteCall(insnaddr, calladdr, stackusage)
        # Reset stack usage to preamble usage
        stackusage = cur.basic_stack_usage

    # Update for known indirect functions
    funcsbyname = {}
    for info in funcs.values():
        funcnameroot = info.funcname.split('.')[0]
        funcsbyname[funcnameroot] = info
    mainfunc = funcsbyname.get('sched_main')
    cmdfunc = funcsbyname.get('command_task')
    eventfunc = funcsbyname.get('__vector_13')
    for funcnameroot, info in funcsbyname.items():
        if (funcnameroot.startswith('_DECL_taskfuncs_')
            or funcnameroot.startswith('_DECL_initfuncs_')
            or funcnameroot.startswith('_DECL_shutdownfuncs_')):
            funcname = funcnameroot[funcnameroot.index('_', 7)+1:]
            f = funcsbyname[funcname]
            mainfunc.noteCall(0, f.funcaddr, mainfunc.basic_stack_usage + 2)
        if funcnameroot.startswith('parser_'):
            f = funcsbyname.get(funcnameroot[7:])
            if f is not None:
                numparams = int(datalines[info.funcaddr][2], 16)
                stackusage = cmdfunc.basic_stack_usage + 2 + numparams * 4
                cmdfunc.noteCall(0, f.funcaddr, stackusage)
        if funcnameroot.endswith('_event'):
            eventfunc.noteCall(0, info.funcaddr, eventfunc.basic_stack_usage + 2)

    # Calculate maxstackusage
    for info in funcs.values():
        calcmaxstack(info, funcs)

    # Sort functions for output
    funcinfos = orderfuncs(funcs.keys(), funcs.copy())

    # Show all functions
    print(OUTPUTDESC)
    for info in funcinfos:
        if info.max_stack_usage == 0 and info.max_yield_usage < 0:
            continue
        yieldstr = ""
        if info.max_yield_usage >= 0:
            yieldstr = ",%d" % info.max_yield_usage
        print("\n%s[%d,%d%s]:" % (info.funcname, info.basic_stack_usage
                                  , info.max_stack_usage, yieldstr))
        for insnaddr, calladdr, stackusage in info.called_funcs:
            callinfo = funcs.get(calladdr, unknownfunc)
            yieldstr = ""
            if callinfo.max_yield_usage >= 0:
                yieldstr = ",%d" % (stackusage + callinfo.max_yield_usage)
            print("    %04s:%-40s [%d+%d,%d%s]" % (
                insnaddr, callinfo.funcname, stackusage
                , callinfo.basic_stack_usage
                , stackusage+callinfo.max_stack_usage, yieldstr))

if __name__ == '__main__':
    main()
