from __future__ import annotations

import dataclasses
import logging
import pathlib
from typing import (Any, ClassVar, Dict, Generator, Iterable, List, Optional,
                    Tuple, Union)

import sphinx
import sphinx.application
import sphinx.environment
from docutils import nodes
from docutils.parsers.rst import directives
from sphinx import addnodes
from sphinx.directives import ObjectDescription
from sphinx.domains import Domain, Index, ObjType
from sphinx.locale import _ as l_
from sphinx.roles import XRefRole
from sphinx.util.docfields import Field, GroupedField
from sphinx.util.nodes import make_refnode
from sphinx.util.typing import OptionSpec

from . import summary, util
from .parse import parse

logger = logging.getLogger(__name__)


MODULE_PATH = pathlib.Path(__file__).parent.resolve()
STATIC_PATH = MODULE_PATH / "docs"
DEFAULT_CSS_FILE = STATIC_PATH / "blark_default.css"


@dataclasses.dataclass
class BlarkSphinxCache:
    cache: Dict[pathlib.Path, summary.CodeSummary] = dataclasses.field(
        default_factory=dict
    )
    _instance_: ClassVar[BlarkSphinxCache]

    @staticmethod
    def instance():
        if not hasattr(BlarkSphinxCache, "_instance_"):
            BlarkSphinxCache._instance_ = BlarkSphinxCache()
        return BlarkSphinxCache._instance_

    def find_by_name(self, name: str):
        for item in self.cache.values():
            try:
                return item.function_blocks[name]
            except KeyError:
                ...

            try:
                return item.functions[name]
            except KeyError:
                ...

        raise KeyError(f"{name!r} not found")

    def configure(self, app: sphinx.application.Sphinx, config):
        for filename in config.blark_projects:
            logger.debug("Loading %s", filename)
            for fn, info in parse(filename):
                logger.debug("Parsed %s", fn)
                self.cache[fn] = summary.CodeSummary.from_source(info)


class BlarkDirective(ObjectDescription[Tuple[str, str]]):
    # From SphinxRole:
    #: The role name actually used in the document.
    name: str
    #: A string containing the entire interpreted text input.
    rawtext: str
    #: The interpreted text content.
    text: str
    #: The line number where the interpreted text begins.
    lineno: int
    #: The ``docutils.parsers.rst.states.Inliner`` object.
    # inliner: Inliner
    #: A dictionary of directive options for customization ("role" directive).
    options: Dict
    #: A list of strings, the directive content for customization ("role" directive).
    content: List[str]

    # From ObjectDescription:
    doc_field_types: List[Field] = []
    domain: Optional[str] = None
    objtype: Optional[str] = None
    indexnode: addnodes.index = None

    # Customizing ObjectDescription:
    has_content: ClassVar[bool] = True
    required_arguments: ClassVar[int] = 1
    optional_arguments: ClassVar[int] = 0
    final_argument_whitespace: ClassVar[bool] = True
    doc_field_types = []
    option_spec: ClassVar[OptionSpec] = {
        "noblocks": directives.flag,
        "nolinks": directives.flag,
        "nosource": directives.flag,
        "noindex": directives.flag,
        "noindexentry": directives.flag,
        "canonical": directives.unchanged,
        "annotation": directives.unchanged,
    }


def declaration_to_signature(
    signode: addnodes.desc_signature,
    obj: summary.DeclarationSummary,
    *,
    env: Optional[sphinx.environment.BuildEnvironment] = None,
):
    if env is not None:
        signode["ids"] = [obj.qualified_name]
        signode["docname"] = env.docname
        signode["qualified_name"] = obj.qualified_name
        env.domaindata["bk"]["declaration"].setdefault(obj.qualified_name, []).append(
            signode
        )
    yield addnodes.desc_sig_name(obj.name, obj.name)
    yield addnodes.desc_sig_punctuation(text=" : ")
    yield addnodes.pending_xref(
        obj.base_type, nodes.Text(obj.type), refdomain="bk",
        reftype="type", reftarget=obj.base_type
    )


def declaration_to_content(obj: summary.DeclarationSummary):
    if obj.value:
        default = nodes.paragraph(text="Default:")
        default += addnodes.literal_strong(text=str(obj.value))
        yield default

    if obj.location:
        location = nodes.paragraph(
            text=f"Linkable {obj.location_type}: "
        )
        location += addnodes.literal_strong(text=str(obj.location))
        yield location

    for comment in obj.comments:
        yield nodes.paragraph(comment, text=util.remove_comment_characters(comment))


def declarations_to_block(
    declarations: Iterable[summary.DeclarationSummary],
    *,
    env: Optional[sphinx.environment.BuildEnvironment] = None,
) -> Generator[addnodes.desc, None, None]:
    # These nodes translate into the following in html:
    # desc -> dl
    # desc_signature -> dt
    # desc_content -> dd
    # So:
    #  desc
    #  -> desc_signature
    #      -> desc_sig_name, desc_sig_punctuation, etc.
    #  -> desc_content
    #      -> paragraph, etc.
    for decl in declarations:
        desc = addnodes.desc(classes=["declaration"])
        signode = addnodes.desc_signature()
        signode += declaration_to_signature(signode, decl, env=env)

        decl_info = addnodes.desc_content()
        decl_info += declaration_to_content(decl)

        desc += signode
        desc += decl_info

        yield desc


class DeclarationDirective(BlarkDirective):
    block_header: str
    obj: summary.DeclarationSummary

    def handle_signature(self, sig: str, signode: addnodes.desc_signature) -> Tuple[str, str]:
        # def transform_content(self, contentnode: addnodes.desc_content) -> None:
        func = self.env.ref_context["bk:function"]
        variable = sig
        self.obj = func.declarations[variable]
        signode += declaration_to_signature(signode, self.obj, env=self.env)
        return sig, func.name

    def transform_content(self, contentnode: addnodes.desc_content) -> None:
        contentnode += declaration_to_content(self.obj)


class VariableBlockDirective(BlarkDirective):
    block_header: str
    parent_name: str
    declarations: List[summary.DeclarationSummary]

    def handle_signature(
        self, sig: str, signode: addnodes.desc_signature
    ) -> Tuple[str, str]:
        self.block_header = sig.upper()
        func = self.env.ref_context["bk:function"]
        self.parent_name = func.name
        self.declarations = list(func.declarations_by_block[self.block_header].values())
        signode += addnodes.desc_name(
            text=self.block_header, classes=["variable_block", self.block_header]
        )
        signode.classes = ["variable_block"]
        return self.block_header, ""

    def transform_content(self, contentnode: addnodes.desc_content) -> None:
        contentnode += declarations_to_block(self.declarations, env=self.env)


def _build_table_from_lists(
    table_data: List[List[nodes.Element]],
    col_widths: List[int],
    *,
    header_rows: int = 1,
    stub_columns: int = 0,
) -> nodes.table:
    """Create a docutils table from a list of elements."""
    table = nodes.table()
    tgroup = nodes.tgroup(cols=len(col_widths))
    table += tgroup
    for col_width in col_widths:
        colspec = nodes.colspec(colwidth=col_width)
        if stub_columns:
            colspec.attributes["stub"] = 1
            stub_columns -= 1
        tgroup += colspec

    def _to_row(row: List[nodes.Element]) -> nodes.row:
        row_node = nodes.row()
        for cell in row:
            entry = nodes.entry()
            entry += cell
            row_node += entry
        return row_node

    rows = list(_to_row(row) for row in table_data)
    if header_rows:
        thead = nodes.thead()
        thead += rows[:header_rows]
        tgroup += thead

    tbody = nodes.tbody()
    tbody += rows[header_rows:]
    tgroup += tbody
    return table


def _to_link_table(
    parent_name: str, decls: Iterable[summary.DeclarationSummary]
) -> nodes.table:
    def decl_items() -> Generator[Tuple[nodes.paragraph, nodes.Text], None, None]:
        for decl in sorted(decls, key=lambda decl: decl.name):
            if decl.location:
                location = " ".join(decl.location.split(" ")[1:]).strip()
            else:
                location = "?"  # shouldn't technically get here

            paragraph = nodes.paragraph()
            paragraph += addnodes.pending_xref(
                decl.qualified_name,
                nodes.Text(decl.name),
                refdomain="bk",
                reftype="declaration",
                reftarget=decl.qualified_name,
            )
            yield paragraph, nodes.Text(location)

    return _build_table_from_lists(
        table_data=[
            [nodes.Text("Name"), nodes.Text("Link")],
            *decl_items()
        ],
        col_widths=[50, 50],
        header_rows=1
    )


class BlarkDirectiveWithDeclarations(BlarkDirective):
    obj: Union[summary.FunctionSummary, summary.FunctionBlockSummary]
    doc_field_types = [
        GroupedField(
            "declaration",
            label=l_("VAR"),
            names=("declaration", ),
            rolename="declaration",
            can_collapse=True,
        ),
        # GroupedField(
        #     "variable_block",
        #     label=l_("VAR"),
        #     names=("variable_block", "var", ),
        #     rolename="variable_block",
        #     typerolename="variable_block",
        #     typenames=("variable_block", "var"),
        #     can_collapse=True,
        # ),
    ]

    def handle_signature(self, sig: str, signode: addnodes.desc_signature) -> Tuple[str, str]:
        """Transform a signature/object into RST nodes."""
        try:
            self.obj = BlarkSphinxCache.instance().find_by_name(sig)
        except KeyError:
            logger.error(
                "Could not find object: %r (signatures unsupported)", sig
            )
            raise ValueError(f"Code object not found: {sig!r}")

        self.env.ref_context["bk:function"] = self.obj

        signode["ids"] = [sig]
        signode["docname"] = self.env.docname
        signode["qualified_name"] = sig
        domain_data = self.env.domaindata["bk"][self.signature_prefix.lower()]
        domain_data.setdefault(sig, []).append(signode)
        sig_prefix = self.get_signature_prefix(sig)
        signode += addnodes.desc_annotation(str(sig_prefix), '', *sig_prefix)
        signode += addnodes.desc_name(self.obj.name, self.obj.name)

        paramlist = addnodes.desc_parameterlist("paramlist")

        for block in ("VAR_INPUT", "VAR_IN_OUT", "VAR_OUTPUT"):
            decls = self.obj.declarations_by_block.get(block, {})
            for variable, decl in decls.items():
                node = addnodes.desc_parameter()
                # node += addnodes.desc_sig_operator('', '*')
                node += addnodes.desc_type("", decl.type)
                node += addnodes.desc_sig_space()
                node += addnodes.desc_sig_name("", variable)
                if block == "VAR_OUTPUT":
                    node += addnodes.desc_sig_punctuation(text="=>")

                paramlist += node

        signode += paramlist

        if getattr(self.obj, "return_type", None) is not None:
            signode += addnodes.desc_returns()
            signode += addnodes.desc_type(text=self.obj.return_type)

        prefix = ""
        return sig, prefix

    def before_content(self) -> None:
        self.env.ref_context['bk:obj'] = self.obj

    def _get_links(self) -> Generator[addnodes.desc, None, None]:
        """Get the linkable inputs/outputs as sphinx nodes."""
        linkable = summary.get_linkable_declarations(
            self.obj.declarations.values()
        )
        for attr in ("input", "output", "memory"):
            decls = getattr(linkable, attr, [])
            if decls:
                block_desc = addnodes.desc(classes=["linkable"])
                name = {
                    "input": "Inputs",
                    "output": "Outputs",
                    "memory": "Memory",
                }[attr]
                sig = addnodes.desc_signature(
                    classes=[f"linkable_{attr}"],
                    ids=[f"{self.obj.name}._linkable_{attr}_"],
                )
                sig += addnodes.desc_name(text=f"Linkable {name}")
                block_desc += sig

                block_desc += _to_link_table(self.obj.name, decls)
                yield block_desc

    def _get_basic_variable_blocks(self) -> Generator[addnodes.desc, None, None]:
        """Get the usual input/output variable blocks as sphinx nodes."""
        for block in ("VAR_INPUT", "VAR_IN_OUT", "VAR_OUTPUT"):
            decls = self.obj.declarations_by_block.get(block, {})
            if not decls:
                continue

            block_desc = addnodes.desc()
            block_desc += addnodes.desc_name(
                text=block, classes=["variable_block", block]
            )
            block_contents = addnodes.desc_content()
            block_contents += declarations_to_block(decls.values(), env=self.env)

            block_desc += block_contents
            yield block_desc

    def _get_source(self) -> Generator[nodes.container, None, None]:
        """Get the usual input/output variable blocks as sphinx nodes."""
        yield nodes.container(
            "",
            nodes.literal_block(self.obj.source_code, self.obj.source_code),
            classes=["plc_source"],
        )

    def transform_content(self, contentnode: addnodes.desc_content) -> None:
        if "nolinks" not in self.options:
            contentnode += self._get_links()

        if "noblocks" not in self.options:
            contentnode += self._get_basic_variable_blocks()

        if "nosource" not in self.options:
            contentnode += self._get_source()

    def get_signature_prefix(self, sig: str) -> List[nodes.Node]:
        return [
            addnodes.desc_sig_keyword(text=self.signature_prefix),
        ]


class FunctionDirective(BlarkDirectiveWithDeclarations):
    obj: summary.FunctionSummary
    signature_prefix: ClassVar[str] = "FUNCTION"
    doc_field_types = list(BlarkDirectiveWithDeclarations.doc_field_types) + [
        Field(
            "returntype",
            label=l_("Return type"),
            has_arg=False,
            names=("rtype",),
            bodyrolename="obj",
        ),
    ]

    def needs_arglist(self) -> bool:
        return True


class FunctionBlockDirective(BlarkDirectiveWithDeclarations):
    obj: summary.FunctionBlockSummary
    signature_prefix: ClassVar[str] = "FUNCTION_BLOCK"


class BlarkXRefRole(XRefRole):
    def process_link(self, env, refnode, has_explicit_title, title, target):
        refnode["bk:scope"] = list(env.ref_context.get("bk:scope", []))
        if not has_explicit_title:
            title = title.lstrip(".")  # only has a meaning for the target
            target = target.lstrip("~")  # only has a meaning for the title
            # if the first character is a tilde, don't display the module/class
            # parts of the contents
            if title.startswith("~"):
                title = title.lstrip("~")
                dot = title.rfind(".")
                if dot != -1:
                    title = title[dot + 1:]
        return title, target


class BlarkDomain(Domain):
    """
    Blark IEC61131-3 language domain.
    """
    name = "bk"
    label = "Blark"
    object_types: ClassVar[Dict[str, ObjType]] = {
        "function_block": ObjType(l_("functionblock"), l_("function_block"), l_("fb")),
        "function": ObjType(l_("function"), l_("func")),
        "type": ObjType(l_("type"), "type"),
        "module": ObjType(l_("module"), "mod"),
        "variable_block": ObjType(l_("variable_block"), "var"),
        "source_code": ObjType(l_("source_code"), "plc_source"),
        "declaration": ObjType(l_("declaration"), "declaration"),
    }

    directives: ClassVar[Dict[str, BlarkDirective]] = {
        "function_block": FunctionBlockDirective,
        "function": FunctionDirective,
        "variable_block": VariableBlockDirective,
        "declaration": DeclarationDirective,
        # "type": Type,
    }

    roles: Dict[str, BlarkXRefRole] = {
        "function_block": BlarkXRefRole(fix_parens=False),
        "function": BlarkXRefRole(fix_parens=False),
        "fb": BlarkXRefRole(fix_parens=False),
        "type": BlarkXRefRole(),
        "mod": BlarkXRefRole(),
        "declaration": BlarkXRefRole(),
    }

    initial_data: ClassVar[str, Dict[str, Any]] = {
        "module": {},
        "type": {},
        "function": {},
        "declaration": {},
        "function_block": {},
        "method": {},
        "action": {},
    }
    indices: List[Index] = [
        # BlarkModuleIndex,
    ]

    def find_obj(self, rolename, node, targetstring):
        for typename, objtype in self.object_types.items():
            if rolename in objtype.roles:
                break
        else:
            return []
        # TODO: scoping?
        # parent_obj = self.env.ref_context.get("bk:function", None)
        # print("scope", parent_obj, rolename, node)
        domaindata = self.env.domaindata["bk"][typename]
        # print("scope", rolename, node, list(domaindata))
        return domaindata.get(targetstring, [])

    def resolve_xref(self, env, fromdocname, builder, typ, target, node, contnode):
        matches = self.find_obj(typ, node, target)
        if not matches:
            logger.warning("No target found for cross-reference: %s", target)
            return None
        if len(matches) > 1:
            logger.warning(
                "More than one target found for cross-reference " "%r: %s",
                target,
                ", ".join(match["qualified_name"] for match in matches),
            )
        match = matches[0]
        return make_refnode(
            builder, fromdocname, match["docname"], match["qualified_name"],
            contnode, target
        )

    def clear_doc(self, docname):
        for name in self.initial_data:
            for name, methods in self.env.domaindata["bk"][name].items():
                to_delete = []
                for idx, method in enumerate(methods):
                    if method["docname"] == docname:
                        to_delete.insert(0, idx)
                for idx in to_delete:
                    methods.pop(idx)


def _initialize_domain(app: sphinx.application.Sphinx, config):
    """Callback function for 'config-inited'."""
    cache = BlarkSphinxCache.instance()
    cache.configure(app, config)


def setup(app: sphinx.application.Sphinx):
    app.add_config_value('blark_projects', [], 'html')
    app.add_config_value('blark_signature_show_type', True, 'html')

    app.add_domain(BlarkDomain)
    app.connect("config-inited", _initialize_domain)
