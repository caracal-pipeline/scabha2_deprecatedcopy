import os.path, re, stat, itertools, logging
from typing import Any, List, Dict, Optional, Union
from enum import Enum
from dataclasses import dataclass, field
from omegaconf.omegaconf import MISSING, OmegaConf
from collections import OrderedDict

from .exceptions import CabValidationError, DefinitionError, SchemaError
from . import validate
from .validate import validate_parameters

## almost supported by omegaconf, see https://github.com/omry/omegaconf/issues/144, for now just use Any
ListOrString = Any   

def EmptyDictDefault():
    return field(default_factory=lambda:OrderedDict())

def EmptyListDefault():
    return field(default_factory=lambda:[])


Conditional = Optional[str]


@dataclass 
class ParameterPolicies(object):
    # if true, value is passed as a positional argument, not an option
    positional: Optional[bool] = None
    # if true, value is head-positional, i.e. passed *before* any options
    positional_head: Optional[bool] = None
    # for list-type values, use this as a separator to paste them together into one argument. Otherwise:
    #  * use "list" to pass list-type values as multiple arguments (--option X Y)
    #  * use "repeat" to rpeat the option (--option X --option Y)
    repeat: Optional[str] = None
    # prefix for non-positional arguments
    prefix: Optional[str] = None

    # skip this parameter
    skip: bool = False
    # if True, implicit parameters will be skipped automatically
    skip_implicits: bool = True

    # if set, a string-type value will be split into a list of arguments using this separator
    split: Optional[str] = None

    # Value formatting policies.
    # If set, specifies {}-type format strings used to convert the value(s) to string(s).
    # For a non-list value:
    #   * if 'format_list' is set, formatts the value into a lisyt of strings as fmt[i].format(value, **dict)
    #     example:  ["{0}", "{0}"] will simply repeat the value twice
    #   * if 'format' is set, value is formatted as format.format(value, **dict) 
    # For a list-type value:
    #   * if 'format_list' is set, each element #i formatted separately as fmt[i].format(*value, **dict)
    #     example:  ["{0}", "{1}"] will simply 
    #   * if 'format' is set, each element #i is formatted as format.format(value[i], **dict) 
    # **dict contains all parameters passed to a cab, so these can be used in the formatting
    format: Optional[str] = None
    format_list: Optional[List[str]] = None



@dataclass 
class CabManagement:        # defines common cab management behaviours
    environment: Optional[Dict[str, str]] = EmptyDictDefault()
    cleanup: Optional[Dict[str, ListOrString]]     = EmptyDictDefault()   
    wranglers: Optional[Dict[str, ListOrString]]   = EmptyDictDefault()   


@dataclass
class Parameter(object):
    """Parameter (of cab or recipe)"""
    info: str = ""
    # for input parameters, this flag indicates a read-write (aka input-output aka mixed-mode) parameter e.g. an MS
    writeable: bool = False
    # data type
    dtype: str = "str"
    # for file-type parameters, specifies that the filename is implicitly set inside the step (i.e. not a free parameter)
    implicit: Optional[str] = None
    # optonal list of arbitrary tags, used to group parameters
    tags: List[str] = EmptyListDefault()

    # if True, parameter is required
    required: bool = False

    # choices for an option-type parameter (should this be List[str]?)
    choices:  Optional[List[Any]] = ()

    # inherited from Stimela 1 -- used to handle paremeters inside containers?
    # might need a re-think, but we can leave them in for now  
    alias: Optional[str] = ""
    pattern: Optional[str] = MISSING

    policies: ParameterPolicies = ParameterPolicies()

@dataclass
class Cargo(object):
    name: Optional[str] = None                    # cab name. (If None, use image or command name)
    info: Optional[str] = None                    # description
    inputs: Dict[str, Parameter] = EmptyDictDefault()
    outputs: Dict[str, Parameter] = EmptyDictDefault()
    defaults: Dict[str, Any] = EmptyDictDefault()

    def __post_init__(self):
        for name in self.inputs.keys():
            if name in self.outputs:
                raise DefinitionError(f"{name} appears in both inputs and outputs")
        self.params = {}
        self._inputs_outputs = None
        # pausterized name
        self.name_ = re.sub(r'\W', '_', self.name or "")  # pausterized name

    @property
    def inputs_outputs(self):
        if self._inputs_outputs is None:
            self._inputs_outputs = self.inputs.copy()
            self._inputs_outputs.update(**self.outputs)
        return self._inputs_outputs
    
    @property
    def invalid_params(self):
        return [name for name, value in self.params.items() if type(value) is validate.Error]

    @property
    def missing_params(self):
        return {name: schema for name, schema in self.inputs_outputs.items() if schema.required and name not in self.params}

    def finalize(self, config, full_name=None, log=None):
        self.log = log

    def validate(self, config, params: Optional[Dict[str, Any]] = None, subst: Optional[Dict[str, Any]] = None):
        pass

    def update_parameter(self, name, value):
        self.params[name] = value

    def make_substitition_namespace(self):
        ns = {name: str(value) for name, value in self.params.items()}
        ns.update(**{name: "MISSING" for name in self.missing_params})
        return OmegaConf.create(ns)


@dataclass 
class Cab(Cargo):
    """Represents a cab i.e. an atomic task in a recipe.
    See dataclass fields below for documentation of fields.

    Additional attributes available after validation with arguments:

        self.input_output:      combined parameter dict (self.input + self.output), maps name to Parameter
        self.missing_params:    dict (name to Parameter) of required parameters that have not been specified
    
    Raises:
        CabValidationError: [description]
    """
    # if set, the cab is run in a container, and this is the image name
    # if not set, commands are run nativelt
    image: Optional[str] = None                   

    # command to run, inside the container or natively
    command: str = MISSING                        # command to run (inside or outside the container)

    # if set, activates this virtual environment first before running the command (not much sense doing this inside the container)
    virtual_env: Optional[str] = None

    # # not sure why this is here, let's retire (recipe defines "dirs")
    # msdir: Optional[bool] = False
    # cab management and cleanup definitions
    management: CabManagement = CabManagement()

    # default parameter conversion policies
    policies: ParameterPolicies = ParameterPolicies()

    wrangler_actions =  {attr: value for attr, value in logging.__dict__.items() if attr.upper() == attr and type(value) is int}

    # then add litetal constants for other wrangler actions
    ACTION_SUPPRESS = wrangler_actions["SUPPRESS"] = "SUPPRESS"
    ACTION_DECLARE_SUCCESS = wrangler_actions["DECLARE_SUCCESS"] = "DECLARE_SUPPRESS"
    ACTION_DECLARE_FAILURE = wrangler_actions["DECLARE_FAILURE"] = "DECLARE_FAILURE"


    def __post_init__ (self):
        if self.name is None:
            self.name = self.image or self.command.split()[0]
        Cargo.__post_init__(self)
        for param in self.inputs.keys():
            if param in self.outputs:
                raise CabValidationError(f"cab {self.name}: parameter {param} is both an input and an output, this is not permitted")
        # setup wranglers
        self._wranglers = []
        for match, actions in self.management.wranglers.items():
            replace = None
            if type(actions) is str:
                actions = [actions]
            if type(actions) is not list:
                raise CabValidationError(f"wrangler entry {match}: expected action or list of actions")
            for action in actions:
                if action.startswith("replace:"):
                    replace = action.split(":", 1)[1]
                elif action not in self.wrangler_actions:
                    raise CabValidationError(f"wrangler entry {match}: unknown action '{action}'")
            actions = [self.wrangler_actions[act] for act in actions if act in self.wrangler_actions]
            try:
                rexp = re.compile(match)
            except Exception as exc:
                raise CabValidationError(f"wrangler entry {match} is not a valid regular expression")
            self._wranglers.append((re.compile(match), replace, actions))
        self._runtime_status = None


    def validate(self, config, params: Optional[Dict[str, Any]] = None, subst: Optional[Dict[str, Any]] = None):
        self.params = validate_parameters(params, self.inputs_outputs, defaults=self.defaults, subst=subst)


    @property
    def summary(self):
        lines = [f"cab {self.name}:"] 
        for name, value in self.params.items():
            # if type(value) is validate.Error:
            #     lines.append(f"  {name} = ERR: {value}")
            # else:
            lines.append(f"  {name} = {value}")
                
        lines += [f"  {name} = ???" for name in self.missing_params.keys()]
        return lines


    def build_command_line(self, subst=None):
        subst = subst or OmegaConf.create()
        subst.self = self.make_substitition_namespace()

        if self.virtual_env:
            venv = os.path.expanduser(self.virtual_env).format(**subst)
            if not os.path.isfile(f"{venv}/bin/activate"):
                raise CabValidationError(f"virtual environment {venv} doesn't exist", log=self.log)
            self.log.debug(f"virtual envirobment is {venv}")
        else:
            venv = None

        command = os.path.expanduser(self.command).format(**subst)
        # collect command
        if "/" not in command:
            from scabha.proc_utils import which
            command0 = command
            command = which(command, extra_paths=venv and [f"{venv}/bin"])
            if command is None:
                raise CabValidationError(f"{command0}: not found", log=self.log)
        else:
            if not os.path.isfile(command) and os.stat(command).st_mode & stat.S_IXUSR:
                raise CabValidationError(f"{command} doesn't exist or is not executable", log=self.log)

        self.log.debug(f"command is {command}")

        return ([command] + self.build_argument_list()), venv


    def build_argument_list(self):
        """
        Converts command, and current dict of parameters, into a list of command-line arguments.

        pardict:     dict of parameters. If None, pulled from default config.
        positional:  list of positional parameters, if any
        mandatory:   list of mandatory parameters.
        repeat:      How to treat iterable parameter values. If a string (e.g. ","), list values will be passed as one
                    command-line argument, joined by that separator. If True, list values will be passed as
                    multiple repeated command-line options. If None, list values are not allowed.
        repeat_dict: Like repeat, but defines this behaviour per parameter. If supplied, then "repeat" is used
                    as the default for parameters not in repeat_dict.

        Returns list of arguments.
        """

        # collect parameters

        value_dict = dict(**self.params)

        def get_policy(schema, policy):
            if schema.policies[policy] is not None:
                return schema.policies[policy]
            else:
                return self.policies[policy]

        def stringify_argument(name, value, schema, option=None):
            if value is None:
                return None
            if schema.dtype == "bool" and not value:
                return None

            is_list = hasattr(value, '__iter__') and type(value) is not str
            format_policy = get_policy(schema, 'format')
            format_list_policy = get_policy(schema, 'format_list')
            split_policy = get_policy(schema, 'split')
            
            if type(value) is str and split_policy:
                value = value.split(split_policy or None)
                is_list = True

            if is_list:
                # apply formatting policies
                if format_list_policy:
                    if len(format_list_policy) != len(value):
                        raise SchemaError("length of format_list_policy does not match length of '{name}'")
                    value = [fmt.format(*value, **value_dict) for fmt in format_list_policy]
                elif format_policy:
                    value = [format_policy.format(x, **value_dict) for x in value]
                else:
                    value = [str(x) for x in value]
            else:
                if format_list_policy:
                    value = [fmt.format(value, **value_dict) for fmt in format_list_policy]
                    is_list = True
                elif format_policy:
                    value = format_policy.format(value, **value_dict)
                else:
                    value = str(value)

            if is_list:
                # check repeat policy and form up representation
                repeat_policy = get_policy(schema, 'repeat')
                if repeat_policy == "list":
                    return [option] + list(value) if option else list(value)
                elif repeat_policy == "repeat":
                    return list(itertools.chain([option, x] for x in value)) if option else list(value)
                elif type(repeat_policy) is str:
                    return [option, repeat_policy.join(value)] if option else repeat_policy.join(value)
                elif repeat_policy is None:
                    raise TypeError(f"list-type parameter '{name}' does not have a repeat policy set")
                else:
                    raise TypeError(f"unknown repeat policy '{repeat_policy}'")
            else:
                return [option, value] if option else [value]

        # check for missing parameters and collect positionals

        pos_args = [], []

        for name, schema in self.inputs_outputs.items():
            if schema.required and name not in value_dict:
                raise RuntimeError(f"required parameter '{name}' is missing")
            if name in value_dict:
                positional_first = get_policy(schema, 'positional_head') 
                positional = get_policy(schema, 'positional') or positional_first
                skip = get_policy(schema, 'skip') or (schema.implicit and get_policy(schema, 'skip_implicits'))
                if positional:
                    if not skip:
                        pargs = pos_args[0 if positional_first else 1]
                        value = stringify_argument(name, value_dict[name], schema)
                        if type(value) is list:
                            pargs += value
                        elif value is not None:
                            pargs.append(value)
                    value_dict.pop(name)

        args = []
                    
        # now check for optional parameters that remain in the dict
        for name, value in value_dict.items():
            if name not in self.inputs_outputs:
                raise RuntimeError(f"unknown parameter '{name}'")
            schema = self.inputs_outputs[name]

            skip = get_policy(schema, 'skip') or (schema.implicit and get_policy(schema, 'skip_implicits'))
            if skip:
                continue

            option = (get_policy(schema, 'prefix') or "--") + (schema.alias or name)

            # True values map to a single option
            if schema.dtype == "bool" and value:
                args.append(option)
            else:
                value = stringify_argument(name, value, schema, option=option)
                if type(value) is list:
                    args += value
                elif value is not None:
                    args.append(value)

        return pos_args[0] + args + pos_args[1]


    @property
    def runtime_status(self):
        return self._runtime_status

    def reset_runtime_status(self):
        self._runtime_status = None

    def apply_output_wranglers(self, output, severity):
        suppress = False
        modified_output = output
        for regex, replace, actions in self._wranglers:
            if regex.search(output):
                if replace is not None:
                    modified_output = regex.sub(replace, output)
                for action in actions:
                    if type(action) is int:
                        severity = action
                    elif action is self.ACTION_SUPPRESS:
                        suppress = True
                    elif action is self.ACTION_DECLARE_FAILURE and self._runtime_status is None:
                        self._runtime_status  = False
                        modified_output = "[FAILURE] " + modified_output
                        severity = logging.ERROR
                    elif action is self.ACTION_DECLARE_SUCCESS and self._runtime_status is None:
                        self._runtime_status = True
                        modified_output = "[SUCCESS] " + modified_output
        return (None, 0) if suppress else (modified_output, severity)