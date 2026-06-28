import re
from simpleparty.icons import icon, ICONS

def test_known_names_render_svg():
    for name in ['download','trash','star','star-outline','embed','tag','wand','gear',
                 'play','prev','next','shuffle','folder','lock','lock-open','check','x','warning','clock','film']:
        svg = icon(name)
        assert svg.startswith('<svg') and svg.rstrip().endswith('</svg>')
        assert 'currentColor' in svg

def test_decorative_icon_is_aria_hidden():
    assert 'aria-hidden="true"' in icon('trash')
    assert 'aria-label' not in icon('trash')

def test_labelled_icon_exposes_name():
    svg = icon('trash', label='Delete')
    assert 'role="img"' in svg and 'aria-label="Delete"' in svg
    assert 'aria-hidden' not in svg

def test_unknown_name_raises():
    import pytest
    with pytest.raises(KeyError):
        icon('not-an-icon')

def test_custom_class_applied():
    assert 'class="star-icon"' in icon('star', cls='star-icon')
