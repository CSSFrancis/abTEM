import numpy as np
import pytest

from abtem.bases import Grid


def test_create_grid():
    grid = Grid(extent=5, sampling=.2)

    print(grid.gpts)
    assert np.all(grid.extent == 5.)
    assert np.all(grid.gpts == 25)

    assert np.all(grid.sampling == .2)

    grid = Grid(sampling=.2, gpts=10)

    assert np.all(np.isclose(grid.extent, 2.))

    grid = Grid(extent=(8, 6), gpts=10)
    assert np.allclose(grid.sampling, np.array([0.8, 0.6]))

    grid = Grid()
    with pytest.raises(RuntimeError):
        grid.check_is_defined()


def test_change_free_grid():
    grid = Grid(extent=(8, 6), gpts=10)

    grid.sampling = .2
    assert np.allclose(grid.extent, np.array([8., 6.]))
    assert np.allclose(grid.gpts, np.array([40, 30]))

    grid.gpts = 100
    assert np.allclose(grid.extent, np.array([8, 6]))
    assert np.allclose(grid.sampling, np.array([0.08, 0.06]))

    grid.extent = (16, 12)
    assert np.allclose(grid.gpts, np.array([100, 100]))
    assert np.allclose(grid.extent, np.array([16, 12]))
    assert np.allclose(grid.sampling, np.array([16 / 100, 12 / 100]))

    grid.extent = (10, 10)
    assert np.allclose(grid.sampling, grid.extent / grid.gpts)

    grid.sampling = .3
    assert np.allclose(grid.extent, grid.sampling * grid.gpts)

    grid.gpts = 30
    assert np.allclose(grid.sampling, grid.extent / grid.gpts)


def test_grid_raises():
    with pytest.raises(RuntimeError) as e:
        Grid(extent=[5, 5, 5])

    assert str(e.value) == 'grid value length of 3 != 2'


def test_grid_notify():
    grid = Grid()

    grid.extent = 5
    assert grid.changed._notify_count == 1

    grid.gpts = 100
    assert grid.changed._notify_count == 2

    grid.sampling = .1
    assert grid.changed._notify_count == 3


def test_locked_grid():
    grid = Grid(gpts=5, lock_gpts=True)

    grid.extent = 10
    assert np.all(grid.sampling == 2)
    grid.extent = 20
    assert np.all(grid.sampling == 4)

    with pytest.raises(RuntimeError) as e:
        grid.gpts = 6

    assert str(e.value) == 'gpts cannot be modified'


def test_check_grid_matches():
    grid1 = Grid(extent=10, gpts=10)
    grid2 = Grid(extent=10, gpts=10)

    grid1.check_can_match(grid2)

    grid2.sampling = .2

    with pytest.raises(RuntimeError) as e:
        grid1.check_can_match(grid2)

    assert str(e.value) == 'inconsistent grid gpts ([10 10] != [50 50])'
