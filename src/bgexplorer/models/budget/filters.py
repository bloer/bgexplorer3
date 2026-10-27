""" Which SourceTerms to include in a budget breakdown """
import json
from typing import Optional

GROUPBY = ('component', 'isotope', 'material', 'category')


class BudgetFilter:
    """ Which SourceTerms to include in a budget breakdown.

    `entries` holds, for each groupby, a list of (key, exclude) pairs.
    A SourceTerm is rejected if it matches any excluded key of a groupby,
    else accepted if that groupby has no included keys or it matches one of
    them. Component keys are paths of placement ids from the object being
    broken down, and match all SourceTerms below that placement.
    `root` is a component path that all SourceTerms must be below; the
    component breakdown shows its children.
    """
    def __init__(self, entries: Optional[dict] = None, root=()):
        self.entries = {groupby: [(self._key(groupby, key), bool(exclude))
                                  for key, exclude in values]
                        for groupby, values in (entries or {}).items()
                        if values}
        self.root = tuple(str(pid) for pid in root)

    @staticmethod
    def _key(groupby: str, key):
        if groupby == 'component':
            return tuple(str(pid) for pid in key)
        return str(key)

    @classmethod
    def from_json(cls, text: Optional[str]) -> 'BudgetFilter':
        """ Parse `{"root": [pid, ...], "<groupby>": [{"key": ...,
        "exclude": false}, ...]}`. Raises ValueError if it isn't valid
        """
        if not text:
            return cls()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"filters are not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("filters must be an object")
        root = data.pop('root', None) or []
        if not isinstance(root, list):
            raise ValueError("filter root must be a list of placement ids")
        entries = {}
        for groupby, values in data.items():
            if groupby not in GROUPBY:
                raise ValueError(f"Unknown filter '{groupby}'")
            if not isinstance(values, list):
                raise ValueError(f"filter '{groupby}' must be a list")
            entries[groupby] = []
            for value in values:
                key = value.get('key') if isinstance(value, dict) else None
                if key is None or (groupby == 'component') != \
                        isinstance(key, list):
                    raise ValueError(f"Invalid '{groupby}' filter {value!r}")
                entries[groupby].append((key, value.get('exclude', False)))
        return cls(entries, root)

    def todict(self) -> dict:
        result = {groupby: [dict(key=list(key) if isinstance(key, tuple)
                                 else key, exclude=exclude)
                            for key, exclude in values]
                  for groupby, values in self.entries.items()}
        if self.root:
            result['root'] = list(self.root)
        return result

    @staticmethod
    def _match(groupby: str, key, test) -> bool:
        if groupby == 'component':
            return key[:len(test)] == test
        return key == test

    def passes(self, groupby: str, key) -> bool:
        """ Whether a SourceTerm with `key` passes `groupby`'s entries """
        entries = self.entries.get(groupby)
        if not entries:
            return True
        if any(exclude and self._match(groupby, key, test)
               for test, exclude in entries):
            return False
        includes = [test for test, exclude in entries if not exclude]
        return not includes or any(self._match(groupby, key, test)
                                   for test in includes)

    def below_root(self, keys: dict) -> bool:
        """ Whether a SourceTerm with `keys` is below `root` """
        return keys['component'][:len(self.root)] == self.root

    def matches(self, keys: dict, skip: Optional[str] = None) -> bool:
        """ Whether a SourceTerm with `keys` passes all entries except
        `skip`'s and is below `root`
        """
        return self.below_root(keys) and all(
            self.passes(groupby, keys[groupby])
            for groupby in self.entries if groupby != skip)
