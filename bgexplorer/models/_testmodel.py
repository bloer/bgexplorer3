from mongoengine import Document, StringField, ListField, ReferenceField, PULL

from fields import QuantityField, HistogramField, Histogram, AsymmetricError as AE

class MyTest(Document):
    name = StringField()
    value = QuantityField()
    spectrum = HistogramField()

    def __repr__(self):
        return f'<MyTest({self.name}, {self.value}, {self.spectrum}>'

    def __str__(self):
        return repr(self)

class BgSource(Document):
    name = StringField()

class Assay(Document):
    name = StringField()
    sources = ListField(ReferenceField(BgSource, reverse_delete_rule=PULL))

if __name__ == '__main__':
    from mongoengine import connect
    from asymmetric import AsymmetricError as AE
    import numpy as np
    print(np.__version__)
    import pint
    units = pint.UnitRegistry()
    connect('bgexplorer3')
    MyTest.drop_collection()
    BgSource.drop_collection()
    Assay.drop_collection()
    U = MyTest(name='U238', value=AE(10, 1.1)*units('mBq/kg'),
               spectrum=Histogram(AE.fromcounts(np.arange(100))*units('1/s**2')))
    #Th = MyTest(name='Th232', value=AE.fromlimit(7)*units('mBq/kg'))
    #K = MyTest(name='K40', value=AE(5,3,2)*units('mBq/kg'))
    #Hist = MyTest(name='hist', value=AE.fromcounts(np.arange(3)))

    U.save()
    #Th.save()
    #K.save()
    #Hist.save()

    for item in MyTest.objects:
        print(item)


    #print("AAAAH!")
    #print(Hist.value)


    U = BgSource(name="U")
    Th = BgSource(name="Th")
    K = BgSource(name="K")
    #U.save()
    #Th.save()
    #K.save()
    assay = Assay(name="test", sources=[U, Th, K])
    assay.save()

    Th.delete()
    assay.reload()
    for source in assay.sources:
        print(source.name)


