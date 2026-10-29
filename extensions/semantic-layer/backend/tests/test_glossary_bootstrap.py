from types import SimpleNamespace

from catalog_source import compile_into, module_function


def test_glossary_bootstrap_creates_parent_tables_before_children_with_checkfirst():
    namespace = compile_into(_migration_namespace(), module_function("_migrate_dictionary_tables"))

    namespace["_migrate_dictionary_tables"]()
    namespace["_migrate_dictionary_tables"]()

    glossary_calls = [call for call in namespace["calls"] if call[0].startswith("Glossary")]
    assert [name for name, _ in glossary_calls] == [
        "GlossaryTerm",
        "GlossaryTermRevision",
        "GlossaryTermLink",
        "GlossaryTermAuditEvent",
    ] * 2
    assert all(checkfirst for _, checkfirst in glossary_calls)


def test_expert_rule_bootstrap_creates_parent_tables_before_children_with_checkfirst():
    namespace = compile_into(_migration_namespace(), module_function("_migrate_dictionary_tables"))

    namespace["_migrate_dictionary_tables"]()

    expert_calls = [call for call in namespace["calls"] if call[0].startswith("ExpertRule")]
    assert [name for name, _ in expert_calls] == [
        "ExpertRule",
        "ExpertRuleRevision",
        "ExpertRuleLink",
        "ExpertRuleAuditEvent",
    ]
    assert all(checkfirst for _, checkfirst in expert_calls)


def _migration_namespace():
    calls = []

    class Table:
        def __init__(self, name):
            self.name = name

        def create(self, *, bind, checkfirst):
            calls.append((self.name, checkfirst))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalars=list, all=list)

    engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"), begin=lambda: Connection())
    db = SimpleNamespace(engine=engine)
    inspect = lambda _engine: SimpleNamespace(
        get_columns=lambda _name: [],
        get_indexes=lambda _name: [],
        has_table=lambda _name: False,
    )
    names = (
        "DataLayer",
        "DatasetDictionary",
        "ColumnDictionary",
        "DatasetGrain",
        "BusinessDomain",
        "LineageEdge",
        "BusinessCanvas",
        "LineageCanvas",
        "MetricContract",
        "MetricContractEvent",
        "MetricDefinition",
        "RelationshipContract",
        "GlossaryTerm",
        "GlossaryTermRevision",
        "GlossaryTermLink",
        "GlossaryTermAuditEvent",
        "ExpertRule",
        "ExpertRuleRevision",
        "ExpertRuleLink",
        "ExpertRuleAuditEvent",
        "DatasetProposal",
    )
    namespace = {name: SimpleNamespace(__tablename__=name, __table__=Table(name)) for name in names}
    namespace.update(
        db=db,
        inspect=inspect,
        text=lambda value: value,
        business_domain_dataset=Table("business_domain_dataset"),
        calls=calls,
    )
    return namespace
