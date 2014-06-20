from collections import defaultdict as ddict

from . import ast
from ..ssa import objtypes
from .. import graph_util

# The basic block in our temporary CFG
# instead of code, it merely contains a list of defs and uses
# This is an extended basic block, i.e. it only terminates in a normal jump(s).
# exceptions can be thrown from various points within the block
class DUBlock(object):
    def __init__(self, key):
        self.key = key
        self.caught_excepts = ()
        self.lines = []     # 3 types of lines: ('use', var), ('def', (var, var2_opt)), or ('canthrow', None)
        self.e_successors = []
        self.n_successors = []

    def canThrow(self): return ('canthrow', None) in self.lines

def varOrNone(expr):
    return expr if isinstance(expr, ast.Local) else None

def canThrow(expr):
    if isinstance(expr, (ast.ArrayAccess, ast.ArrayCreation, ast.Cast, ast.ClassInstanceCreation, ast.FieldAccess, ast.MethodInvocation)):
        return True
    if isinstance(expr, ast.BinaryInfix) and expr.opstr in ('/','%'): #check for possible division by 0
        return expr.dtype not in (objtypes.FloatTT, objtypes.DoubleTT)
    return False

def visitExpr(expr, lines):
    if expr is None:
        return
    if isinstance(expr, ast.Local):
        lines.append(('use', expr))

    if isinstance(expr, ast.Assignment):
        lhs, rhs = map(varOrNone, expr.params)

        #with assignment we need to only visit LHS if it isn't a local in order to avoid spurious uses
        #also, we need to visit RHS before generating the def
        if lhs is None:
            visitExpr(expr.params[0], lines)
        visitExpr(expr.params[1], lines)
        if lhs is not None:
            lines.append(('def', (lhs, rhs)))
    else:
        for param in expr.params:
            visitExpr(param, lines)

    if canThrow(expr):
        lines.append(('canthrow', None))

class DUGraph(object):
    def __init__(self):
        self.blocks = []
        self.entry = None

    def makeBlock(self, key, break_dict, caught_except, myexcept_parents):
        block = DUBlock(key)
        self.blocks.append(block)

        for parent in break_dict[block.key]:
            parent.n_successors.append(block)
        del break_dict[block.key]

        assert((myexcept_parents is None) == (caught_except is None))
        if caught_except is not None: #this is the head of a catch block:
            block.caught_excepts = (caught_except,)
            for parent in myexcept_parents:
                parent.e_successors.append(block)
        return block

    def finishBlock(self, block, catch_stack):
        #register exception handlers for completed old block
        if block.canThrow():
            for clist in catch_stack:
                clist.append(block)

    def visitScope(self, scope, isloophead, break_dict, catch_stack, caught_except=None, myexcept_parents=None):
        #catch_stack is copy on modify
        head_block = block = self.makeBlock(scope.continueKey, break_dict, caught_except, myexcept_parents)

        for stmt in scope.statements:
            if isinstance(stmt, (ast.ExpressionStatement, ast.ThrowStatement, ast.ReturnStatement)):
                visitExpr(stmt.expr, block.lines)
                if isinstance(stmt, ast.ThrowStatement):
                    block.lines.append(('canthrow', None))
                continue

            #compound statements
            assert(stmt.continueKey is not None)
            if isinstance(stmt, (ast.IfStatement, ast.SwitchStatement)):
                visitExpr(stmt.expr, block.lines)
                jumps = [sub.continueKey for sub in stmt.getScopes()]

                if isinstance(stmt, ast.SwitchStatement):
                    ft = not stmt.hasDefault()
                else:
                    ft = len(jumps) == 1
                if ft:
                    jumps.append(stmt.breakKey)

                for sub in stmt.getScopes():
                    break_dict[sub.continueKey].append(block)
                    self.visitScope(sub, False, break_dict, catch_stack)

            elif isinstance(stmt, ast.WhileStatement):
                assert(stmt.expr == ast.Literal.TRUE)
                assert(stmt.continueKey == stmt.getScopes()[0].continueKey)
                break_dict[stmt.continueKey].append(block)
                self.visitScope(stmt.getScopes()[0], True, break_dict, catch_stack)

            elif isinstance(stmt, ast.TryStatement):
                new_stack = catch_stack + [[] for _ in stmt.pairs]

                break_dict[stmt.tryb.continueKey].append(block)
                self.visitScope(stmt.tryb, False, break_dict, new_stack)

                for cdecl, catchb in stmt.pairs:
                    parents = new_stack.pop()
                    self.visitScope(catchb, False, break_dict, catch_stack, cdecl.local, parents)
                assert(new_stack == catch_stack)
            else:
                assert(isinstance(stmt, ast.StatementBlock))
                break_dict[stmt.continueKey].append(block)
                self.visitScope(stmt, False, break_dict, catch_stack)

            if stmt.breakKey is not None:
                self.finishBlock(block, catch_stack)
                # start new block after return from compound statement
                block = self.makeBlock(stmt.breakKey, break_dict, None, None)
            else:
                block = None #should never be accessed anyway if we're exiting abruptly

        if scope.jumpKey is not None:
            break_dict[scope.jumpKey].append(block)

        if isloophead: #special case - if scope is the contents of a loop, we need to check for backedges
            # assert(scope.continueKey != scope.breakKey)
            head_block.n_successors += break_dict[scope.continueKey]
            del break_dict[scope.continueKey]

        if block is not None:
            self.finishBlock(block, catch_stack)

    def makeCFG(self, root):
        break_dict = ddict(list)
        self.visitScope(root, False, break_dict, [])
        self.entry = self.blocks[0] #entry point should always be first block generated

        reached = graph_util.topologicalSort([self.entry], lambda block:(block.n_successors + block.e_successors))
        if len(reached) != len(self.blocks):
            print 'warning, {} blocks unreachable!'.format(len(self.blocks) - len(reached))
        self.blocks = reached

    def replace(self, old, new):
        assert(old != new)
        for block in self.blocks:
            assert(old not in block.caught_excepts)
            lines = block.lines
            for i, (line_t, data) in enumerate(lines):
                if line_t == 'use' and data == old:
                    lines[i] = 'use', new
                elif line_t == 'def':
                    v1 = new if data[0] == old else data[0]
                    v2 = new if data[1] == old else data[1]
                    lines[i] = 'def', (v1, v2)

    def simplify(self):
        #try to prune redundant instructions from blocks
        for block in self.blocks:
            last = None
            newlines = []
            for line in block.lines:
                if line[0] == 'def':
                    if line[1][0] == line[1][1]:
                        continue
                elif line == last:
                    continue
                newlines.append(line)
                last = line
            block.lines = newlines