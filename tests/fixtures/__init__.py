from pathlib import Path

BOI_FIXTURES = Path(__file__).parent / "boi"


def boi_fixture(name: str) -> bytes:
    return (BOI_FIXTURES / name).read_bytes()
