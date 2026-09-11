from miggy.operations import MigrateOperation
from miggy.serializer import SerializeValueMixin


class OperationWriter(SerializeValueMixin):
    def __init__(self, operation: MigrateOperation, indentation: int = 2) -> None:
        self.operation = operation
        self.buff: list[str] = []
        self.indentation = indentation
        self.imports = set()

    def _write(self, _arg_value, _arg_name=None) -> None:
        _arg_name_prefix = "%s=" % _arg_name if _arg_name else ""
        if isinstance(_arg_value, dict):
            self.feed("%s{" % _arg_name_prefix)
            self.indent()
            for key, value in _arg_value.items():
                key_string = self.serialize_value(key)
                arg_string = self.serialize_value(value)
                self.feed("%s: %s," % (key_string, arg_string))
            self.unindent()
            self.feed("},")
        else:
            arg_string = self.serialize_value(_arg_value)
            self.feed("%s%s," % (_arg_name_prefix, arg_string))

    def serialize(self) -> str:
        operation_call, args, kwargs = self.operation.deconstruct()

        self.feed("%s(" % operation_call)
        self.indent()

        for arg_value in args:
            self._write(arg_value)

        for arg_name, arg_value in kwargs.items():
            self._write(arg_value, arg_name)

        self.unindent()
        self.feed("),")
        return self.render()

    def indent(self) -> None:
        self.indentation += 1

    def unindent(self) -> None:
        self.indentation -= 1

    def feed(self, line) -> None:
        self.buff.append(" " * (self.indentation * 4) + line)

    def render(self) -> str:
        return "\n".join(self.buff)
