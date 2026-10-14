"""Delayed boundary clicks must not move a newer native poster selection."""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'


class EdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree=ast.parse((LIB/'window_handler.py').read_text())
        window=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FilterWindow')
        method=next(n for n in window.body if isinstance(n,ast.FunctionDef) and n.name=='_grid_edge')
        ns={}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'grid_edge','exec'),ns)
        cls.edge=staticmethod(ns['_grid_edge'])

    def owner(self,selected,size=120,total=500):
        self.select=Mock();self.page=Mock()
        return types.SimpleNamespace(_grid_page=True,_is_active=lambda:True,
            getControl=lambda _:types.SimpleNamespace(getSelectedPosition=lambda:selected,
                size=lambda:size,selectItem=self.select),_grid_pending_right=None,
            poster_loader=types.SimpleNamespace(total=total,next_page=self.page))

    def test_delayed_right_boundary_click_cannot_advance_a_newer_middle_selection(self):
        owner=self.owner(7)
        self.edge(owner,1)
        self.select.assert_not_called();self.page.assert_not_called()

    def test_delayed_left_boundary_click_cannot_pull_a_newer_middle_selection_back(self):
        owner=self.owner(10)
        self.edge(owner,-1)
        self.select.assert_not_called();self.page.assert_not_called()

    def test_row_transitions_and_noncycling_edges_remain_intact(self):
        owner=self.owner(5);self.edge(owner,1);self.select.assert_called_once_with(6)
        owner=self.owner(6);self.edge(owner,-1);self.select.assert_called_once_with(5)
        owner=self.owner(0);self.edge(owner,-1);self.select.assert_not_called()
        owner=self.owner(56,57,57);self.edge(owner,1);self.select.assert_not_called()

    def test_last_loaded_item_requests_next_page_without_selecting_out_of_range(self):
        owner=self.owner(119);self.edge(owner,1)
        self.assertEqual(owner._grid_pending_right,120)
        self.page.assert_called_once();self.select.assert_not_called()


if __name__=='__main__':unittest.main()
