# test_sorting.py

import numpy as np
from astropy.table import Table
from astropy.time import Time

# importa e aplica o monkey-patch
from utils import patch_table_sorting
patch_table_sorting()

def main():
    # Cria uma tabela com coluna mixin Time
    t = Table({
        'time': Time(['2020-01-02', '2020-01-01', '2020-01-03']),
        'flux': [1.2, 3.4, 2.5]
    })

    print("=== Tabela original ===")
    print(t)

    # Usa o Table.sort (que agora chama unified_argsort)
    t_sorted = t.sort('time')
    print("\n=== Tabela ordenada por 'time' ===")
    print(t_sorted)

    # Verifica também o índice retornado por argsort
    idx = t.argsort('time')
    print("\nÍndices para ordenação (argsort):", idx)

    # Confirma que o índice ordena corretamente
    sorted_times = t['time'][idx]
    print("Times ordenados via argsort:", sorted_times.iso)  # .iso para mostrar string legível

    # Teste com múltiplas chaves: primeiro por flux decrescente, depois por time
    # (o unified_argsort trata só ascendente, então para decrescente você pode inverter)
    idx2 = t.argsort(('time',))  # exemplo simples
    print("\nTeste múltiplas chaves (time):", idx2)

if __name__ == '__main__':
    main()
