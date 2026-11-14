class Widget:
    @classmethod
    def create(cls, name: str) -> "Widget":
        return cls(name)

    def __init__(self, name: str) -> None:
        self.name = name
