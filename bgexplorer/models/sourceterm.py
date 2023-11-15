""" A background source term is a unique combination of:
- an instance of a component in a particular location (assembly hierarchy)
- An assay source
- a conversion efficieny (simulation result)
"""

def ispositive(value):
    if value <= 0:
        raise ValidationError("Value must be strictly greater than zero")

class Placement(EmbeddedDocument):
    component = ReferenceField(Component, required=True)
    weight = FloatField(default=1, validation=ispositive)
    label = StringField(required=False, max_length=32)
    simvolume = StringField(max_length=128)
    @property
    def name(self):
        return self.label or self.component.name

class SourceTerm(Document):
    assemblyPath = EmbeddedDocumentListField(Placement)
    assay = ReferenceField(Assay)
    sourceIndex = IntField()
    ceff = ReferenceField(ConversionEff)
    values = MapField(QuantityField)
    spectra = MapField(HistogramField)


    @property
    def component(self):
        return self.assemblyPath[-1].component

    @property
    def weight(self):
        wt = 1
        for placement in self.assemblyPath:
            wt = wt * placement.weight
        return wt

    @property
    def source(self):
        return assay.source[self.sourceIndex]

    @property
    def assemblyPathStr(self):
        return [p.component.name for p in self.assemblyPath]
