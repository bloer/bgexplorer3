from mongoengine import Document
from mongoengine.fields import StringField, MapField, UUIDField, ListField
from .fields import UncertainQuantityField, HistogramField

class SimResult(Document):
    """ Result of a Monte Carlo 
