""" Compare documents and whole versions """
from bson import ObjectId
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional
from .verdoc import VersionedDocument, strip_identity

# fields that don't count as content
IGNORED_FIELDS = ('enteredby',)


@dataclass
class DiffEntry:
    """ One difference between two documents. `path` is a list of field
    names and list items, `kind` one of 'added' (only on the right),
    'removed' (only on the left) or 'changed'
    """
    path: List[str]
    kind: str
    left: Any = None
    right: Any = None

    @property
    def pathstr(self) -> str:
        return '.'.join(self.path).replace('.[', '[')


def raw_content(doc: VersionedDocument) -> dict:
    """ The stored content of `doc`, without the fields that identify the
    copy or don't count as content
    """
    son = strip_identity(doc.to_mongo().to_dict())
    for key in IGNORED_FIELDS:
        son.pop(key, None)
    return son


def _is_leaf(value) -> bool:
    """ Values compared as a whole: anything but plain dicts and lists, and
    dicts that store a single value such as a quantity
    """
    if isinstance(value, dict):
        return 'str' in value or 'units' in value
    return not isinstance(value, list)


def _keyed(items: list) -> Optional[Dict[Any, dict]]:
    """ Items of a list of embedded documents by their 'id', or None if
    they aren't all embedded documents with unique ids
    """
    if not all(isinstance(item, dict) and 'id' in item for item in items):
        return None
    keyed = {item['id']: item for item in items}
    return keyed if len(keyed) == len(items) else None


def _label(item: dict, index: int) -> str:
    name = item.get('name') if isinstance(item, dict) else None
    return f"[{name}]" if name else f"[{index}]"


def diff_values(left, right, path=None) -> Iterator[DiffEntry]:
    """ Yield the differences between two raw (`to_mongo`) values """
    path = path or []
    if left == right:
        return
    if left is None or right is None:
        yield DiffEntry(path, 'added' if left is None else 'removed',
                        left, right)
    elif isinstance(left, dict) and isinstance(right, dict) \
            and not (_is_leaf(left) or _is_leaf(right)):
        for key in list(left) + [k for k in right if k not in left]:
            yield from diff_values(left.get(key), right.get(key),
                                   path + [key])
    elif isinstance(left, list) and isinstance(right, list) \
            and not all(map(_is_leaf, left + right)):
        lkeyed, rkeyed = _keyed(left), _keyed(right)
        if lkeyed is not None and rkeyed is not None and (left or right):
            # embedded documents are matched by id, so reordering and
            # inserting don't show up as changes to every item
            for index, item in enumerate(left):
                yield from diff_values(item, rkeyed.get(item['id']),
                                       path + [_label(item, index)])
            for index, item in enumerate(right):
                if item['id'] not in lkeyed:
                    yield DiffEntry(path + [_label(item, index)], 'added',
                                    None, item)
        else:
            for index in range(max(len(left), len(right))):
                litem = left[index] if index < len(left) else None
                ritem = right[index] if index < len(right) else None
                yield from diff_values(litem, ritem,
                                       path + [_label(litem or ritem, index)])
    else:
        yield DiffEntry(path, 'changed', left, right)


def diff_documents(left: VersionedDocument, right: VersionedDocument
                   ) -> List[DiffEntry]:
    """ The differences in content between two copies of a document """
    if left.id == right.id:
        # the same physical copy
        return []
    return list(diff_values(raw_content(left), raw_content(right)))


def find_refs(value) -> Iterator[ObjectId]:
    """ The ids referenced in a raw value. Embedded documents' own 'id's
    aren't references
    """
    if isinstance(value, ObjectId):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in ('id', '_id', 'original_id'):
                yield from find_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from find_refs(item)


@dataclass
class DocumentComparison:
    """ One item (`original_id`) in two versions. `left` and `right` are the
    documents, or None if the item isn't in that version
    """
    original_id: ObjectId
    left: Optional[VersionedDocument]
    right: Optional[VersionedDocument]
    entries: List[DiffEntry] = field(default_factory=list)

    @property
    def same_copy(self) -> bool:
        return (self.left is not None and self.right is not None
                and self.left.id == self.right.id)

    @property
    def identical(self) -> bool:
        return (self.left is not None and self.right is not None
                and not self.entries)


def compare_document(cls, original_id, left: str, right: str,
                     exclude=('attachments__data',)) -> DocumentComparison:
    """ Compare the copies of item `original_id` of class `cls` in versions
    `left` and `right`
    """
    original_id = ObjectId(original_id)
    skip = [f for f in exclude if f.split('__')[0] in cls._fields]

    def load(tag):
        qs = cls.select_version(tag)(original_id=original_id)
        return (qs.exclude(*skip) if skip else qs).first()
    result = DocumentComparison(original_id, load(left), load(right))
    if result.left is not None and result.right is not None:
        result.entries = diff_documents(result.left, result.right)
    return result
