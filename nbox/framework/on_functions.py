# weird file name, yessir. Why? because
# from nbox.framework.on_functions import PureFunctionParser
# read as "from nbox's framework on Functions import the pure-function Parser"

import ast
import inspect
from typing import List, Union
from uuid import uuid4
from logging import getLogger
logger = getLogger()

# ==================

from dataclasses import dataclass

# these classes are for the Op

@dataclass
class ExpressionNodeInfo:
  # visible
  name: str
  code: str
  nbox_string: str

  # hidden
  lineno: int
  col_offset: int
  end_lineno: int
  end_col_offset: int
  
  # visible
  inputs: List = ()
  outputs: List = ()

@dataclass
class IfNodeInfo:
  nbox_string: str
  conditions: List[ExpressionNodeInfo]


@dataclass
class Node:
  # hidden
  id: str
  execution_index: int
  
  # visible
  name: str
  type: str
  
  # different 
  node_info: Union[ExpressionNodeInfo, IfNodeInfo]
  operator: str = "CodeBlock"

  nbox_string: str = None

  def get_dict(self):
    data = self.__dict__.copy()
    node_info = data["node_info"]
    if isinstance(node_info, ExpressionNodeInfo):
      data["node_info"] = node_info.__dict__
    elif isinstance(node_info, list):
      data["node_info"] = [x.__dict__ for x in node_info]
    return data

@dataclass
class Edge:
  id: str
  source: str
  target: str
  type: str = None

  def get_dict(self):
    return self.__dict__.copy()


# ==================
# the next set of functions are meant as support methods to create nbox_strings IR.

class NboxStrings:
  OP_TO_STRING = {
    "function": "FUNCTION: {name} ( {inputs} ) => [ {outputs} ]",
    "define": "DEFINE: {name} ( {inputs} )",
    "for": "FOR: {name} ( {iter} ) => ( {target} )",
  }

  def __init__(self):
    pass

  def function(self, name, inputs, outputs):
    return self.OP_TO_STRING["function"].format(
      name=name,
      inputs=", ".join([f"{x['kwarg']}={x['value']}" for x in inputs]),
      outputs=", ".join(outputs)
    )

  def define(self, name, inputs):
    return self.OP_TO_STRING["define"].format(
      name=name,
      inputs=", ".join([f"{x['kwarg']}={x['value']}" for x in inputs])
    )

  def _for(self, name, iter, target):
    return self.OP_TO_STRING["for"].format(
      name=name,
      iter=iter,
      target=target
    )

nbxl = NboxStrings()

def write_program(nodes):
  for i, n in enumerate(nodes):
    if n.nbox_string == None:
      print(f"{i:03d}|{n.node_info.nbox_string}")
    else:
      print(f"{i:03d}|{n.nbox_string}")

# ==================

def get_code_portion(cl, lineno, col_offset, end_lineno, end_col_offset, **_):
  sl, so, el, eo = lineno, col_offset, end_lineno, end_col_offset
  if sl == el:
    return cl[sl-1][so:eo]
  code = ""
  for i in range(sl - 1, el, 1):
    if i == sl - 1:
      code += cl[i][so:]
    elif i == el - 1:
      code += "\n" + cl[i][:eo]
    else:
      code += "\n" + cl[i]
  
  # convert to base64
  # import base64
  # return base64.b64encode(code.encode()).decode()
  return code

def parse_args(node):
  inputs = []
  for a in node.args:
    a = a.arg if isinstance(a, ast.arg) else a
    inputs.append({
      "kwarg": None,
      "value": a,
    })
  for a in node.kwonlyargs:
    inputs.append({
      "kwarg": a[0],
      "value": a[1],
    })
  if node.vararg:
    inputs.append({
      "kwarg": "*"+node.vararg.arg,
      "value": None
    })
  if node.kwarg:
    inputs.append({
      "kwarg": "**"+node.kwarg.arg,
      "value": None
    })
  return inputs

def get_name(node):
  if isinstance(node, ast.Name):
    return node.id
  elif isinstance(node, ast.Attribute):
    return get_name(node.value) + "." + node.attr
  elif isinstance(node, ast.Call):
    return get_name(node.func)

def parse_kwargs(node, lines):
  if isinstance(node, ast.Name):
    return node.id
  if isinstance(node, ast.Constant):
    val = node.value
    return val
  if isinstance(node, ast.keyword):
    arg = node.arg
    value = node.value
    if 'id' in value.__dict__:
      # arg = my_model
      return (arg, value.id)
    elif 'value' in value.__dict__:
      # arg = 5
      return (arg, value.value)
    elif 'func' in value.__dict__:
      #   arg = some_function(with, some=args)
      #   ^^^   ^^^^^
      # kwarg   value
      return {"kwarg": arg, "value": get_code_portion(lines, **value.__dict__)}
  if isinstance(node, ast.Call):
    return get_code_portion(lines, **node.func.__dict__)

def node_assign_or_expr(node, lines):
  # print(get_code_portion(lines, **node.__dict__))
  value = node.value
  try:
    name = get_name(value.func)
  except AttributeError:
    return None
  args = [parse_kwargs(x, lines) for x in value.args + value.keywords]
  inputs = []
  for a in args:
    if isinstance(a, dict):
      inputs.append(a)
      continue
    inputs.append({
      "kwarg": a[0] if isinstance(a, tuple) else None,
      "value": a[1] if isinstance(a, tuple) else a,
    })

  outputs = []
  if isinstance(node, ast.Assign):
    targets = node.targets[0]
    outputs = [parse_kwargs(x, lines) for x in targets.elts] \
      if isinstance(targets, ast.Tuple) \
      else [parse_kwargs(targets, lines)
    ]

  return ExpressionNodeInfo(
    name = name,
    inputs = inputs,
    outputs = outputs,
    nbox_string = nbxl.function(name, inputs, outputs),
    code = get_code_portion(lines, **node.__dict__),
    lineno = node.lineno,
    col_offset = node.col_offset,
    end_lineno = node.end_lineno,
    end_col_offset = node.end_col_offset,
  )

def node_if_expr(node, lines):
  def if_cond(node, lines, conds = []):
    if not hasattr(node, "test"):
      else_cond = list(filter(lambda x: x["condition"] == "else", conds))
      if not else_cond:
        conds.append({
          "condition": "else",
          "code": dict(
            lineno = node.lineno,
            col_offset = node.col_offset,
            end_lineno = node.end_lineno,
            end_col_offset = node.end_col_offset,
          )
        })
      else:
        cond = else_cond[0]
        cond["code"]["end_lineno"] = node.end_lineno
        cond["code"]["end_col_offset"] = node.end_col_offset
    else:
      condition = get_code_portion(lines, **node.test.__dict__)

      # need to run this last or "else" comes up first
      conds.append({
        "condition": condition,
        "code": {
          "lineno": node.lineno,
          "col_offset": node.col_offset,
          "end_lineno": node.end_lineno,
          "end_col_offset": node.end_col_offset,
        }
      })
      for x in node.orelse:
        if_cond(x, lines, conds)

    return conds
  
  # get all the conditions and structure as ExpressionNodeInfo
  conditions = []
  all_conditions = if_cond(node, lines, conds = [])
  ends = []
  for b0, b1  in zip(all_conditions[:-1], all_conditions[1:]):
    ends.append([b0["code"], b1["code"]])
  for i in range(len(ends)):
    ends[i] = {
      "lineno": ends[i][0]["lineno"],
      "col_offset": ends[i][0]["col_offset"],
      "end_lineno": ends[i][1]["lineno"],
      "end_col_offset": ends[i][1]["col_offset"],
    }
  ends += [all_conditions[-1]["code"]]

  for i, c in enumerate(all_conditions):
    box = ends[i]
    _node = ExpressionNodeInfo(
      name = f"if-{i}",
      nbox_string = c["condition"],
      code = get_code_portion(lines, **box),
      lineno = box['lineno'],
      col_offset = box['col_offset'],
      end_lineno = box['end_lineno'],
      end_col_offset = box['end_col_offset'],
    )
    conditions.append(_node)

  nbox_string = "IF: { " + ", ".join(x.nbox_string for x in conditions) + " }"
  return IfNodeInfo(
    conditions = conditions,
    nbox_string = nbox_string,
  )

def def_func_or_class(node, lines):
  out = {"name": node.name, "code": get_code_portion(lines, **node.__dict__), "type": "def-node"}
  if isinstance(node, ast.FunctionDef):
    out.update({"func": True, "inputs": parse_args(node.args)})
  else:
    out.update({"func": False, "inputs": []})
  return out


# ==================

type_wise_logic = {
  # defns ------
  ast.FunctionDef: def_func_or_class,
  ast.ClassDef: def_func_or_class,

  # nodes ------
  ast.Assign: node_assign_or_expr,
  ast.Expr: node_assign_or_expr,
  ast.If: node_if_expr,

  # todos ------
  # ast.AsyncFunctionDef: async_func_def,
  # ast.Await: node_assign_or_expr,
}

# ==================

def get_nbx_flow(forward):
  # get code string from operator
  code = inspect.getsource(forward).strip()
  node = ast.parse(code)

  edges = [] # this is the flow
  nodes = [] # this is the operators
  symbols_to_nodes = {} # this is things that are defined at runtime

  for i, expr in enumerate(node.body[0].body):
    if not type(expr) in type_wise_logic:
      continue

    output = type_wise_logic[type(expr)](expr, code.splitlines())
    if output is None:
      continue

    if isinstance(output, ExpressionNodeInfo):
      output = Node(
        id = str(uuid4()),
        execution_index = i,
        name = output.name,
        type = "op-node",
        operator = "CodeBlock",
        node_info = output,
      )
      nodes.append(output)
    elif isinstance(output, IfNodeInfo):
      output = Node(
        id = str(uuid4()),
        execution_index = i,
        name = f"if-{i}",
        type = "op-node",
        operator = "Conditional",
        node_info = output.conditions,
        nbox_string = output.nbox_string,
      )
      nodes.append(output)
    elif "def" in output["type"]:
      symbols_to_nodes[output['name']] = {
        "node_info": output,
        "execution_index": i,
        "nbox_string": nbxl.define(output["name"], output["inputs"])
      }

  # edges for execution order can be added
  for op0, op1 in zip(nodes[:-1], nodes[1:]):
    edges.append(
      Edge(
        id = f"edge-{op0.id}-X-{op1.id}",
        source = op0.id,
        target = op1.id,
        type = "execution-order",
    )
  )

  return nodes, edges, symbols_to_nodes
