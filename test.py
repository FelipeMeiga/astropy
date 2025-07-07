#!/usr/bin/env python3.11
import numpy as np
from astropy.table import Table, Column
from astropy.time import Time
from astropy.table.groups import table_group_by, column_group_by

def test_argsort_and_sort():
    t = Table({
        "time": Time(["2020-01-02", "2020-01-01", "2020-01-03"]),
        "flux": [1.2, 3.4, 2.5]
    })

    # 1) argsort deve indicar [1, 0, 2]
    idx = t.argsort("time")
    print("argsort:", idx)
    assert np.array_equal(idx, [1, 0, 2])

    # 2) sort modifica t em-lugar
    t.sort("time")
    print("t após sort:\n", t)
    expected = [
        "2020-01-01 00:00:00.000",
        "2020-01-02 00:00:00.000",
        "2020-01-03 00:00:00.000"
    ]
    assert list(t["time"].iso) == expected

def test_table_group_by_time():
    t = Table({
        "time": Time([
            "2020-01-01", "2020-01-01",
            "2020-01-02", "2020-01-03", "2020-01-03"
        ]),
        "value": [1, 2, 3, 4, 5]
    })

    grouped = table_group_by(t, "time")
    tg = grouped.groups

    # Chaves únicas (iso completo)
    result_keys = [k.iso for k in tg.keys["time"]]
    print("grouped table keys:", result_keys)
    assert result_keys == [
        "2020-01-01 00:00:00.000",
        "2020-01-02 00:00:00.000",
        "2020-01-03 00:00:00.000"
    ]

    # Índices de início de cada grupo
    print("grouped table indices:", list(tg.indices))
    assert list(tg.indices) == [0, 2, 3, 5]

    # Tamanhos de cada grupo
    sizes = [len(g) for g in tg]
    print("group sizes:", sizes)
    assert sizes == [2, 1, 2]

def test_column_group_by_numeric():
    arr = np.array([10, 10, 20, 30, 30, 30])
    col = Column(arr, name="x")
    cg = column_group_by(col, arr).groups

    print("column grouped keys:", list(cg.keys))
    assert list(cg.keys) == [10, 20, 30]

    print("column grouped indices:", list(cg.indices))
    assert list(cg.indices) == [0, 2, 3, 6]

    sizes = [len(cg[i]) for i in range(len(cg))]
    print("column group sizes:", sizes)
    assert sizes == [2, 1, 3]

def test_table_index_sorted_data():
    t = Table({
        "time": Time(["2020-01-02", "2020-01-01", "2020-01-03"]),
        "flux": [1.2, 3.4, 2.5]
    })

    # registra um índice na coluna 'time'
    t.add_index("time")

    # recupera o objeto de índice e chama sorted_data()
    idx_obj = t.indices["time"]
    idx = idx_obj.sorted_data()
    print("index sorted_data:", idx)

    # deve coincidir com t.argsort("time")
    expected = t.argsort("time")
    assert np.array_equal(idx, expected)

if __name__ == "__main__":
    test_argsort_and_sort();            print("test_argsort_and_sort passou")
    test_table_group_by_time();         print("test_table_group_by_time passou")
    test_column_group_by_numeric();     print("test_column_group_by_numeric passou")
    test_table_index_sorted_data();     print("test_table_index_sorted_data passou")
