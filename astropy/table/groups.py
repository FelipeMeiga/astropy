# Licensed under a 3-clause BSD style license - see LICENSE.rst

import warnings

import numpy as np

from astropy.utils.exceptions import AstropyUserWarning

from .index import get_index_by_names

__all__ = ["ColumnGroups", "TableGroups"]


def table_group_by(table, keys):
    # index copies are unnecessary and slow down _table_group_by
    with table.index_mode("discard_on_copy"):
        return _table_group_by(table, keys)


def _table_group_by(table, keys):
    from .serialize import represent_mixins_as_columns
    from .index import get_index_by_names
    from .table import Table

    if isinstance(keys, str):
        keys = (keys,)

    if isinstance(keys, (list, tuple)):
        for name in keys:
            if name not in table.colnames:
                raise ValueError(f"Table does not have key column {name!r}")
            if table.masked and np.any(table[name].mask):
                raise ValueError(f"Missing values in key column {name!r} are not allowed")

        table_keys = table.__class__([table[key] for key in keys], copy=False)
        table_index = get_index_by_names(table, keys)
        grouped_by_table_cols = True if table_index is not None else False

    elif isinstance(keys, (np.ndarray, Table)):
        table_keys = keys
        if len(table_keys) != len(table):
            raise ValueError(
                f"Input keys array length {len(table_keys)} does not match "
                f"table length {len(table)}"
            )
        table_index = None
        grouped_by_table_cols = False

    else:
        raise TypeError(
            f"Keys input must be string, list, tuple, Table or numpy array, "
            f"but got {type(keys)}"
        )

    if table_index is not None:
        idx_sort = table_index.sorted_data()
    else:
        idx_sort = table.argsort(keys)

    table_keys = table_keys[idx_sort]

    diffs = np.concatenate(([True], table_keys[1:] != table_keys[:-1], [True]))
    indices = np.flatnonzero(diffs)

    out = table.__class__(table[idx_sort])
    if len(table) == 0:
        out_keys = table_keys
        indices = np.array([], dtype=int)
    else:
        out_keys = table_keys[indices[:-1]]

    if isinstance(out_keys, Table):
        out_keys.meta["grouped_by_table_cols"] = grouped_by_table_cols
    out._groups = TableGroups(out, indices=indices, keys=out_keys)

    return out


def column_group_by(column, keys):
    from .table import Table

    if isinstance(keys, Table):
        idx_sort = keys.argsort()
    else:
        idx_sort = np.argsort(keys)

    if len(idx_sort) != len(column):
        raise ValueError(
            f"Length of sorted index ({len(idx_sort)}) does not match column length ({len(column)})"
        )

    keys_sorted = keys[idx_sort]

    diffs = np.concatenate(([True], keys_sorted[1:] != keys_sorted[:-1], [True]))
    indices = np.flatnonzero(diffs)

    out = column.__class__(column[idx_sort])
    out._groups = ColumnGroups(out, indices=indices, keys=keys_sorted[indices[:-1]])

    return out


class BaseGroups:
    """
    A class to represent groups within a table of heterogeneous data.

      - ``keys``: key values corresponding to each group
      - ``indices``: index values in parent table or column corresponding to group boundaries
      - ``aggregate()``: method to create new table by aggregating within groups
    """

    @property
    def parent(self):
        return (
            self.parent_column if isinstance(self, ColumnGroups) else self.parent_table
        )

    def __iter__(self):
        self._iter_index = 0
        return self

    def next(self):
        ii = self._iter_index
        if ii < len(self.indices) - 1:
            i0, i1 = self.indices[ii], self.indices[ii + 1]
            self._iter_index += 1
            return self.parent[i0:i1]
        else:
            raise StopIteration

    __next__ = next

    def __getitem__(self, item):
        parent = self.parent

        if isinstance(item, (int, np.integer)):
            i0, i1 = self.indices[item], self.indices[item + 1]
            out = parent[i0:i1]
            out.groups._keys = parent.groups.keys[item]
        else:
            indices0, indices1 = self.indices[:-1], self.indices[1:]
            try:
                i0s, i1s = indices0[item], indices1[item]
            except Exception as err:
                raise TypeError(
                    "Index item for groups attribute must be a slice, "
                    "numpy mask or int array"
                ) from err
            mask = np.zeros(len(parent), dtype=bool)
            # Is there a way to vectorize this in numpy?
            for i0, i1 in zip(i0s, i1s):
                mask[i0:i1] = True
            out = parent[mask]
            out.groups._keys = parent.groups.keys[item]
            out.groups._indices = np.concatenate([[0], np.cumsum(i1s - i0s)])

        return out

    def __repr__(self):
        return f"<{self.__class__.__name__} indices={self.indices}>"

    def __len__(self):
        return len(self.indices) - 1


class ColumnGroups(BaseGroups):
    def __init__(self, parent_column, indices=None, keys=None):
        self.parent_column = parent_column  # parent Column
        self.parent_table = parent_column.info.parent_table
        self._indices = indices
        self._keys = keys

    @property
    def indices(self):
        # If the parent column is in a table then use group indices from table
        if self.parent_table is not None:
            return self.parent_table.groups.indices
        else:
            if self._indices is None:
                return np.array([0, len(self.parent_column)])
            else:
                return self._indices

    @property
    def keys(self):
        # If the parent column is in a table then use group indices from table
        if self.parent_table is not None:
            return self.parent_table.groups.keys
        else:
            return self._keys

    def aggregate(self, func):
        i0s, i1s = self.indices[:-1], self.indices[1:]
        par_col = self.parent_column
        try:
            # Short-cut for cases where .reduceat is known to work well.
            if (
                isinstance(par_col, np.ndarray)
                and not hasattr(par_col, "mask")
                and (hasattr(func, "reduceat") or func is np.sum or func is np.mean)
            ):
                if func is np.mean:
                    vals = np.add.reduceat(par_col, i0s) / np.diff(self.indices)
                else:
                    if func is np.sum:
                        func = np.add
                    vals = func.reduceat(par_col, i0s)
            else:
                # Count on class initializer to be able to concatenate lists.
                vals = [func(par_col[i0:i1]) for i0, i1 in zip(i0s, i1s)]
            out = par_col.__class__(vals)
        except Exception as err:
            raise TypeError(
                f"Cannot aggregate column '{par_col.info.name}' "
                f"with type '{par_col.info.dtype}': {err}"
            ) from err

        out_info = out.info
        for attr in ("name", "unit", "format", "description", "meta"):
            try:
                setattr(out_info, attr, getattr(par_col.info, attr))
            except AttributeError:
                pass

        return out

    def filter(self, func):
        """
        Filter groups in the Column based on evaluating function ``func`` on each
        group sub-table.

        The function which is passed to this method must accept one argument:

        - ``column`` : `Column` object

        It must then return either `True` or `False`.  As an example, the following
        will select all column groups with only positive values::

          def all_positive(column):
              if np.any(column < 0):
                  return False
              return True

        Parameters
        ----------
        func : function
            Filter function

        Returns
        -------
        out : Column
            New column with the aggregated rows.
        """
        mask = np.empty(len(self), dtype=bool)
        for i, group_column in enumerate(self):
            mask[i] = func(group_column)

        return self[mask]


class TableGroups(BaseGroups):
    def __init__(self, parent_table, indices=None, keys=None):
        self.parent_table = parent_table  # parent Table
        self._indices = indices
        self._keys = keys

    @property
    def key_colnames(self):
        """
        Return the names of columns in the parent table that were used for grouping.
        """
        # If the table was grouped by key columns *in* the table then treat those columns
        # differently in aggregation.  In this case keys will be a Table with
        # keys.meta['grouped_by_table_cols'] == True.  Keys might not be a Table so we
        # need to handle this.
        grouped_by_table_cols = getattr(self.keys, "meta", {}).get(
            "grouped_by_table_cols", False
        )
        return self.keys.colnames if grouped_by_table_cols else ()

    @property
    def indices(self):
        if self._indices is None:
            return np.array([0, len(self.parent_table)])
        else:
            return self._indices

    def aggregate(self, func):
        """
        Aggregate each group in the Table into a single row by applying the reduction
        function ``func`` to group values in each column.

        Parameters
        ----------
        func : function
            Function that reduces an array of values to a single value

        Returns
        -------
        out : Table
            New table with the aggregated rows.
        """
        i0s = self.indices[:-1]
        out_cols = []
        parent_table = self.parent_table

        for col in parent_table.columns.values():
            # For key columns just pick off first in each group since they are identical
            if col.info.name in self.key_colnames:
                new_col = col.take(i0s)
            else:
                try:
                    new_col = col.info.groups.aggregate(func)
                except TypeError as err:
                    warnings.warn(str(err), AstropyUserWarning)
                    continue

            out_cols.append(new_col)

        return parent_table.__class__(out_cols, meta=parent_table.meta)

    def filter(self, func):
        """
        Filter groups in the Table based on evaluating function ``func`` on each
        group sub-table.

        The function which is passed to this method must accept two arguments:

        - ``table`` : `Table` object
        - ``key_colnames`` : tuple of column names in ``table`` used as keys for grouping

        It must then return either `True` or `False`.  As an example, the following
        will select all table groups with only positive values in the non-key columns::

          def all_positive(table, key_colnames):
              colnames = [name for name in table.colnames if name not in key_colnames]
              for colname in colnames:
                  if np.any(table[colname] < 0):
                      return False
              return True

        Parameters
        ----------
        func : function
            Filter function

        Returns
        -------
        out : Table
            New table with the aggregated rows.
        """
        mask = np.empty(len(self), dtype=bool)
        key_colnames = self.key_colnames
        for i, group_table in enumerate(self):
            mask[i] = func(group_table, key_colnames)

        return self[mask]

    @property
    def keys(self):
        return self._keys
