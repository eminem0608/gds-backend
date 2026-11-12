"""Production liquid balances, in litres; never treated as saleable stock."""
def liquid_balance(actual, planned, packaged=0, packaging_loss=0, emptied=False):
    measured = actual is not None
    volume = max(float(actual if measured else planned or 0), 0)
    remaining = 0 if emptied else max(volume - float(packaged or 0) - float(packaging_loss or 0), 0)
    return {'volume_l': remaining, 'volume_basis': 'actual' if measured else 'planned',
            'input_volume_l': volume, 'packaged_volume_l': float(packaged or 0),
            'packaging_loss_l': float(packaging_loss or 0)}
