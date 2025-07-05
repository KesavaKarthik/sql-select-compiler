"""Stage 5: SQL-to-Java type mapping and bean emission."""

import pytest

from sql_analyzer import (
    CodeGenerator,
    ResolvedField,
    TypeMapper,
    UnmappedTypeError,
)


@pytest.mark.parametrize("sql_type,java_type", [
    ("varchar", "String"),
    ("char", "String"),
    ("text", "String"),
    ("int", "int"),
    ("tinyint", "int"),
    ("bigint", "long"),
    ("decimal", "BigDecimal"),
    ("numeric", "BigDecimal"),
    ("float", "float"),
    ("double", "double"),
    ("date", "LocalDate"),
    ("datetime", "LocalDateTime"),
    ("timestamp", "LocalDateTime"),
    ("time", "LocalTime"),
    ("boolean", "boolean"),
    ("bit", "boolean"),
])
def test_type_mapping(sql_type, java_type):
    assert TypeMapper().to_java(sql_type) == java_type


def test_type_mapping_is_case_insensitive():
    assert TypeMapper().to_java("VARCHAR") == "String"


def test_unknown_type_fails_loudly():
    """Silently emitting Object would hide a real gap, so this must raise."""
    with pytest.raises(UnmappedTypeError, match="geometry"):
        TypeMapper().to_java("geometry")


@pytest.fixture
def bean():
    fields = [
        ResolvedField("seatNumber", "varchar", "bookings"),
        ResolvedField("price", "decimal", "bookings"),
        ResolvedField("total", "int", None),
    ]
    return CodeGenerator().generate("BookingSummary", fields)


def test_class_declaration(bean):
    assert bean.startswith("public class BookingSummary {")
    assert bean.rstrip().endswith("}")


def test_private_fields_are_declared(bean):
    assert "    private String seatNumber;" in bean
    assert "    private BigDecimal price;" in bean
    assert "    private int total;" in bean


def test_no_argument_constructor(bean):
    assert "    public BookingSummary() {" in bean


def test_all_arguments_constructor(bean):
    assert (
        "    public BookingSummary(String seatNumber, BigDecimal price, int total) {"
        in bean
    )
    assert "        this.seatNumber = seatNumber;" in bean


def test_getters_and_setters_capitalise_correctly(bean):
    assert "    public String getSeatNumber() {" in bean
    assert "    public void setSeatNumber(String seatNumber) {" in bean
    assert "    public int getTotal() {" in bean


def test_accessor_count_matches_field_count(bean):
    assert bean.count("public ") == 1 + 2 + (3 * 2)  # class + 2 ctors + get/set per field


def test_generator_propagates_unmapped_types():
    fields = [ResolvedField("shape", "geometry", "t")]
    with pytest.raises(UnmappedTypeError):
        CodeGenerator().generate("T", fields)


def test_empty_field_list_still_produces_valid_class():
    bean = CodeGenerator().generate("Empty", [])
    assert "public class Empty {" in bean
    assert "    public Empty() {" in bean
