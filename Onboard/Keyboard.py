# -*- coding: utf-8 -*-

# Copyright © 2007 Martin Böhme <martin.bohm@kubuntu.org>
# Copyright © 2007-2009 Chris Jones <tortoise@tortuga>
# Copyright © 2010 Francesco Fumanti <francesco.fumanti@gmx.net>
# Copyright © 2012 Gerd Kohlberger <lowfi@chello.at>
# Copyright © 2009, 2011-2015 marmuta <marmvta@gmail.com>
#
# This file is part of Onboard.
#
# Onboard is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# Onboard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import division, print_function, unicode_literals

import sys
import time
import weakref
import gc
from contextlib import contextmanager

from gi.repository import Gdk

import logging
_logger = logging.getLogger(__name__)

from Onboard.Version import require_gi_versions
require_gi_versions()
try:
    from gi.repository import Atspi
except ImportError as e:
    _logger.warning("Atspi typelib missing, at-spi key-synth unavailable")

from Onboard.KeyGtk                import *
from Onboard                       import KeyCommon
from Onboard.KeyCommon             import StickyBehavior
from Onboard.KeyboardPopups        import TouchFeedback
from Onboard.Sound                 import Sound
from Onboard.ClickSimulator        import ClickSimulator, \
                                          CSButtonMapper, CSFloatingSlave
from Onboard.Scanner               import Scanner
from Onboard.Timer                 import Timer, ProgressiveDelayTimer
from Onboard.utils                 import Modifiers, LABEL_MODIFIERS, \
                                          parse_key_combination, \
                                          unicode_str
from Onboard.definitions           import Handle, UIMask
from Onboard.AutoShow              import AutoShow
from Onboard.AutoHide              import AutoHide
from Onboard.WordSuggestions       import WordSuggestions
from Onboard.canonical_equivalents import canonical_equivalents

try:
    from Onboard.utils import run_script, get_keysym_from_name, dictproperty
except DeprecationWarning:
    pass

### Config Singleton ###
from Onboard.Config import Config
config = Config()
########################


class EventType:
    """ enum of event types for key press/release """
    (
        CLICK,
        DOUBLE_CLICK,
        DWELL,
    ) = range(3)


class DockMode:
    """ enum dock mode """
    (
        FLOATING,
        BOTTOM,
        TOP,
    ) = range(3)


class UnpressTimers:
    """
    Redraw keys unpressed after a short while.
    There are multiple timers to suppurt multi-touch.
    """

    def __init__(self, keyboard):
        self._keyboard = keyboard
        self._timers = {}

    def start(self, key):
        timer = self._timers.get(key)
        if not timer:
            timer = Timer()
            self._timers[key] = timer
        timer.start(config.UNPRESS_DELAY, self.on_timer, key)

    def stop(self, key):
        timer = self._timers.get(key)
        if timer:
            timer.stop()
            del self._timers[key]

    def cancel_all(self):
        for key, timer in self._timers.items():
            Timer.stop(timer)
            key.pressed = False

    def finish(self, key):
        timer = self._timers.get(key)
        if timer:
            timer.stop()
            self.unpress(key)

    def on_timer(self, key):
        self.unpress(key)
        self.stop(key)
        return False

    def unpress(self, key):
        if key.pressed:
            key.pressed = False
            self._keyboard.on_key_unpressed(key)


class KeySynth(object):

    _last_press_time = 0
    _suppress_keypress_delay = False

    @staticmethod
    @contextmanager
    def no_delay():
        """
        Temporarily disable the keypress delay. Do not nest.
        Mainly used for single key-strokes as there are far fewer calls
        for these than the bulk text insertion calls.
        """
        KeySynth._suppress_keypress_delay = True
        yield None
        KeySynth._suppress_keypress_delay = False

    def _delay_keypress(self):
        """
        Pause between multiple key-strokes.
        Firefox and Thunderbird may need this to not miss key-strokes.
        """
        delay = config.keyboard.inter_key_stroke_delay
        if delay:
            if not KeySynth._suppress_keypress_delay: # not just single presses?
                elapsed = time.time() - KeySynth._last_press_time
                remaining = delay - elapsed
                if remaining > 0.0:
                    time.sleep(remaining)

            KeySynth._last_press_time = time.time()


class KeySynthVirtkey(KeySynth):
    """ Synthesize key strokes with python-virtkey """

    def __init__(self, vk):
        self._vk = vk

    def cleanup(self):
        self._vk = None

    def press_unicode(self, char):
        if self._vk:
            if sys.version_info.major == 2:
                char = unicode_str(char)[0]
            code_point = ord(char)
            self._delay_keypress()
            self._vk.press_unicode(code_point)

    def release_unicode(self, char):
        if self._vk:
            if sys.version_info.major == 2:
                char = unicode_str(char)[0]
            code_point = ord(char)
            self._vk.release_unicode(code_point)

    def press_keysym(self, keysym):
        if self._vk:
            self._delay_keypress()
            self._vk.press_keysym(keysym)

    def release_keysym(self, keysym):
        if self._vk:
            self._vk.release_keysym(keysym)

    def press_keycode(self, keycode):
        if self._vk:
            self._delay_keypress()
            self._vk.press_keycode(keycode)

    def release_keycode(self, keycode):
        if self._vk:
            self._vk.release_keycode(keycode)

    def lock_mod(self, mod_mask):
        if self._vk:
            self._vk.lock_mod(mod_mask)

    def unlock_mod(self, mod_mask):
        if self._vk:
            self._vk.unlock_mod(mod_mask)

    def press_key_string(self, keystr):
        """
        Send key presses for all characters in a unicode string.
        """
        keystr = keystr.replace("\\n", "\n")  # for new lines in snippets

        if self._vk:   # may be None in the last call before exiting
            for ch in keystr:
                if ch == "\b":   # backspace?
                    keysym = get_keysym_from_name("backspace")
                    self.press_keysym(keysym)
                    self.release_keysym(keysym)

                elif ch == "\n":
                    # press_unicode("\n") fails in gedit.
                    # -> explicitely send the key symbol instead
                    keysym = get_keysym_from_name("return")
                    self.press_keysym(keysym)
                    self.release_keysym(keysym)
                else:             # any other printable keys
                    self.press_unicode(ch)
                    self.release_unicode(ch)


class KeySynthAtspi(KeySynthVirtkey):
    """
    Synthesize key strokes with AT-SPI

    Not really useful anymore, as key generation there doesn't fit
    Onboard's requirements very well, e.g. there is no consistent
    separation between press and release events.

    Also some unexpected key sequences are not faithfully reproduced.
    """

    def __init__(self, vk):
        super(KeySynthAtspi, self).__init__(vk)

    def press_key_string(self, string):
        if not "Atspi" in globals():
            return
        Atspi.generate_keyboard_event(0, string, Atspi.KeySynthType.STRING)

    def press_keycode(self, keycode):
        if not "Atspi" in globals():
            return
        self._delay_keypress()
        Atspi.generate_keyboard_event(keycode, "", Atspi.KeySynthType.PRESS)

    def release_keycode(self, keycode):
        if not "Atspi" in globals():
            return
        Atspi.generate_keyboard_event(keycode, "", Atspi.KeySynthType.RELEASE)


class TextChanger():
    """
    Insert and delete text by all means available to Onboard.
    - KeySynthVirtkey
    - KeySynthAtspi (not used by default)
    - Direct insertion/deletion via AtspiTextContext
    """

    def __init__(self, keyboard, vk):
        self._keyboard = weakref.ref(keyboard)
        self._key_synth_virtkey = KeySynthVirtkey(vk)
        self._key_synth_atspi = KeySynthAtspi(vk)

        if config.keyboard.key_synth: # == KeySynth.ATSPI:
            self._key_synth = self._key_synth_atspi
        else: # if config.keyboard.key_synth == KeySynth.VIRTKEY:
            self._key_synth = self._key_synth_virtkey

    def cleanup(self):
        # Somehow keyboard objects don't get released
        # when switching layouts, there are still
        # excess references/memory leaks somewhere.
        # We need to manually release virtkey references or
        # Xlib runs out of client connections after a couple
        # dozen layout switches.
        if self._key_synth_virtkey:
            self._key_synth_virtkey.cleanup()
            self._key_synth_virtkey = None
        if self._key_synth_atspi:
            self._key_synth_atspi.cleanup()
            self._key_synth_atspi = None

    def get_keyboard(self):
        return self._keyboard()

    # KeySynth interface
    def press_unicode(self, char):
        self._key_synth.press_unicode(char)

    def release_unicode(self, char):
        self._key_synth.release_unicode(char)

    def press_key_string(self, string):
        self._key_synth.press_key_string(string)

    def press_keycode(self, keycode):
        self._key_synth.press_keycode(keycode)

    def release_keycode(self, keycode):
        self._key_synth.release_keycode(keycode)

    def press_keysym(self, keysym):
        self._key_synth.press_keysym(keysym)

    def release_keysym(self, keysym):
        self._key_synth.release_keysym(keysym)

    def lock_mod(self, mod):
        self._key_synth.lock_mod(mod)

    def unlock_mod(self, mod):
        self._key_synth.unlock_mod(mod)

    # Higher-level functions
    def press_keysyms(self, key_name, count=1):
        """
        Generate any number of full key-strokes for the given named key symbol.
        """
        keysym = get_keysym_from_name(key_name)
        for i in range(count):
            self.press_keysym(keysym)
            self.release_keysym(keysym)

    def insert_string_at_caret(self, text, allow_insertion=True):
        """
        Insert text at the caret position.
        """
        text_context = self.get_keyboard().text_context
        if allow_insertion and text_context.can_insert_text():
            text = text.replace("\\n", "\n")
            text_context.insert_text_at_caret(text)
        else:
            self._key_synth.press_key_string(text)

    def delete_at_caret(self):
        keyboard = self.get_keyboard()
        text_context = keyboard.text_context
        if text_context.can_insert_text():
            text_context.delete_text_before_caret()
        else:
            with keyboard.suppress_modifiers():
                self.press_keysyms("backspace")


class Keyboard(WordSuggestions):
    """ Cairo based keyboard widget """

    color_scheme = None

    _layer_locked = False
    _last_alt_key = None
    _alt_locked   = False
    _text_changer = None
    _click_sim    = None

### Properties ###

    # The number of pressed keys per modifier
    _mods                 = {1:0,2:0, 4:0,8:0, 16:0,32:0,64:0,128:0}

    # Same to keep track of modifier changes triggerd from the outside.
    # Doesn't include modifier changes caused by Onboard itself, so this is
    # not a complete representation of the modifier state.
    _external_mod_changes = {1:0,2:0, 4:0,8:0, 16:0,32:0,64:0,128:0}

    def _get_mod(self, key):
        return self._mods[key]
    def _set_mod(self, key, value):
        self._mods[key] = value
        self.on_mods_changed()
    mods = dictproperty(_get_mod, _set_mod)

    def on_mods_changed(self):
        pass

    def get_mod_mask(self):
        """ Bit-mask of curently active modifers. """
        return sum(mask for mask in (1<<bit for bit in range(8)) \
                   if self.mods[mask])  # bit mask of current modifiers

    @contextmanager
    def suppress_modifiers(self, modifiers = LABEL_MODIFIERS):
        """ Turn modifiers off temporarily. May be nested. """
        self._push_and_clear_modifiers(modifiers)
        yield None
        self._pop_and_restore_modifiers()

    def _push_and_clear_modifiers(self, modifiers):
        mods = {mod : key for mod, key in self._mods.items()
                if mod & modifiers}
        self._suppress_modifiers_stack.append(mods)
        for mod, nkeys in mods.items():
            if nkeys:
                self._mods[mod] = 0
                self._text_changer.unlock_mod(mod)

    def _pop_and_restore_modifiers(self):
        mods = self._suppress_modifiers_stack.pop()
        for mod, nkeys in mods.items():
            if nkeys:
                self._mods[mod] = nkeys
                self._text_changer.lock_mod(mod)

    # currently active layer
    def _get_active_layer_index(self):
        return config.active_layer_index
    def _set_active_layer_index(self, index):
        config.active_layer_index = index
    active_layer_index = property(_get_active_layer_index,
                                  _set_active_layer_index)

    def _get_active_layer(self):
        layers = self.get_layers()
        if not layers:
            return None
        index = self.active_layer_index
        if index < 0 or index >= len(layers):
            index = 0
        return layers[index]
    def _set_active_layer(self, layer):
        index = 0
        for i, layer in enumerate(self.get_layers()):
            if layer is layer:
                index = i
                break
        self.active_layer_index = index
    active_layer = property(_get_active_layer, _set_active_layer)

    def assure_valid_active_layer(self):
        """
        Reset layer index if it is out of range. e.g. due to
        loading a layout with fewer panes.
        """
        index = self.active_layer_index
        if index < 0 or index >= len(self.get_layers()):
            self.active_layer_index = 0

##################

    def __init__(self, application):
        WordSuggestions.__init__(self)

        self._application = weakref.ref(application)
        self._pressed_key = None
        self._last_typing_time = 0

        self._temporary_modifiers = None
        self._locked_temporary_modifiers = None
        self._suppress_modifiers_stack = []
        self._capitalization_requested = False

        self.layout = None
        self.scanner = None
        self.button_controllers = {}
        self.editing_snippet = False

        self._layout_views = []

        self._unpress_timers = UnpressTimers(self)
        self._touch_feedback = TouchFeedback()

        self._raise_timer = ProgressiveDelayTimer()

        self._auto_show = AutoShow(self)
        self._auto_show.enable(config.is_auto_show_enabled())
        self._auto_hide = AutoHide(self)
        self._auto_hide.enable(config.is_auto_hide_enabled())

        self._text_changer = None
        self._key_synth_virtkey = None
        self._key_synth_atspi = None

        self._invalidated_ui = 0

        self._pressed_keys = []
        self._latched_sticky_keys = []
        self._locked_sticky_keys = []
        self._non_modifier_released = False
        self._disabled_keys = None

        self._pending_modifier_redraws = {}
        self._pending_modifier_redraws_timer = Timer()

        self.reset()

    def reset(self):
        """ init/reset on layout change """
        WordSuggestions.reset(self)

        if self._auto_show:
            self._auto_show.reset()

        self.stop_raise_attempts()

        # Keep caps-lock state on layout change to prevent LP #1313176.
        # Otherwise, a caps press causes a layout change, cleanup
        # triggers another caps press that again causes a layout change,
        # and so on...
        # See OnboardGtk.reload_layout_delayed for the other part of the
        # puzzle for this bug report.
        self.ignore_capslock()

        # reset still latched and locked modifier keys on exit
        self.release_latched_sticky_keys()

        # NumLock is special. Keep its state on exit, except when
        # sticky_key_release_delay is set, then we assume to be
        # in kiosk mode and everything has to be cleaned up.
        release_all = bool(config.keyboard.sticky_key_release_delay)
        self.release_locked_sticky_keys(release_all)

        self.release_pressed_keys()

        self._pressed_keys = []
        self._latched_sticky_keys = []
        self._locked_sticky_keys = []
        self._non_modifier_released = False
        self._disabled_keys = None

        self.layout = None

        self._pending_modifier_redraws_timer.stop()
        self._pending_modifier_redraws = {}

    def cleanup(self):
        """ final cleanup on exit """
        self.reset()

        WordSuggestions.cleanup(self)

        if self._auto_show:
            self._auto_show.cleanup()
            self._auto_show = None

        if self._auto_hide:
            self._auto_hide.cleanup()
            self._auto_hide = None

        if self._text_changer:
            self._text_changer.cleanup()
            self._text_changer = None

        if self._click_sim:
            self._click_sim.cleanup()
            self._click_sim = None

    def get_application(self):
        return self._application()

    def register_view(self, layout_view):
        self._layout_views.append(layout_view)

    def deregister_view(self, layout_view):
        if layout_view in self._layout_views:
            self._layout_views.remove(layout_view)

    def get_main_view(self):
        layout_views = self._layout_views
        if layout_views:
            return layout_views[0]
        return None

    def is_visible(self):
        for view in self._layout_views:
            visible = view.is_visible()
            if not visible is None:
                return visible

    def set_visible(self, visible):
        self.update_auto_show_on_visibility_change(visible)
        for view in self._layout_views:
            view.set_visible(visible)

    def toggle_visible(self):
        """ main method to show/hide onboard manually """
        self.set_visible(not self.is_visible())

    def redraw(self, keys = None, invalidate = True):
        for view in self._layout_views:
            view.redraw(keys, invalidate)

    def process_updates(self):
        for view in self._layout_views:
            view.process_updates()

    def redraw_labels(self, invalidate = True):
        for view in self._layout_views:
            view.redraw_labels(invalidate)

    def has_input_sequences(self):
        for view in self._layout_views:
            if view.has_input_sequences():
                return True
        return False

    def update_transparency(self):
        for view in self._layout_views:
            view.update_transparency()

    def update_input_event_source(self):
        """ Input event source changed, tell all views. """
        for view in self._layout_views:
            view.update_input_event_source()
        self.update_click_sim()
        self.update_auto_hide()

    def update_touch_input_mode(self):
        """ Touch input mode has changed, tell all views. """
        for view in self._layout_views:
            view.update_touch_input_mode()

    def update_click_sim(self):
        if config.is_event_source_xinput():
            # XInput click simulator
            # Recommended, but requires the XInput event source.
            clicksim = CSFloatingSlave(self)

            # Fall back to button mapper if XInput 2.2 is unavaliable
            if not clicksim.is_valid():
                _logger.warning("XInput click simulator CSFloatingSlave "
                                "unavailable, "
                                "falling back to CSButtonMapper.")
                clicksim = CSButtonMapper()

        else:
            # Button mapper
            # Works with any event source, but may fail on touch-screens.
            clicksim = CSButtonMapper()

        if self._click_sim:
            self._click_sim.cleanup()
        self._click_sim = clicksim
        self._click_sim.state_notify_add(self._on_click_sim_state_notify)

        _logger.info("using click simulator '{}'" \
                     .format(type(self._click_sim).__name__))

    def _on_click_sim_state_notify(self, x):
        self.invalidate_context_ui()
        self.commit_ui_updates()

    def show_touch_handles(self, show, auto_hide = True):
        for view in self._layout_views:
            view.show_touch_handles(show, auto_hide)

    def set_layout(self, layout, color_scheme, vk):
        """ set or replace the current keyboard layout """
        self.reset()
        self.set_virtkey(vk)
        self.layout = layout
        self.color_scheme = color_scheme
        self.on_layout_loaded()

    def on_layout_loaded(self):
        """ called when the layout has been loaded """

        # hide all still visible feedback popups; keys have changed.
        self._touch_feedback.hide()

        self._connect_button_controllers()
        self.assure_valid_active_layer()

        WordSuggestions.on_layout_loaded(self)

        self.update_modifiers()

        self.update_scanner_enabled()

        # notify views
        for view in self._layout_views:
            view.on_layout_loaded()

        # redraw everything
        self.invalidate_ui()
        self.commit_ui_updates()

    def set_virtkey(self, vk):
        self._text_changer = TextChanger(self, vk)

    def _connect_button_controllers(self):
        """ connect button controllers to button keys """
        self.button_controllers = {}

        # connect button controllers to button keys
        types = { type.id : type for type in \
                   [BCMiddleClick, BCSingleClick, BCSecondaryClick,
                    BCDoubleClick, BCDragClick, BCHoverClick,
                    BCHide, BCShowClick, BCMove, BCPreferences, BCQuit,
                    BCExpandCorrections, BCPreviousPredictions,
                    BCNextPredictions, BCPauseLearning, BCLanguage,
                    BCStealthMode, BCAutoLearn, BCAutoPunctuation, BCInputline,
                   ]
                }
        for key in self.layout.iter_global_keys():
            if key.is_layer_button():
                bc = BCLayer(self, key)
                bc.layer_index = key.get_layer_index()
                self.button_controllers[key] = bc
            else:
                type = types.get(key.id)
                if type:
                    self.button_controllers[key] = type(self, key)

    def update_scanner_enabled(self):
        """ Enable keyboard scanning if it is enabled in gsettings. """
        self.update_input_event_source()
        self.enable_scanner(config.scanner.enabled)

    def enable_scanner(self, enable):
        """ Enable keyboard scanning. """
        if enable:
            if not self.scanner:
                self.scanner = Scanner(self._on_scanner_redraw,
                                       self._on_scanner_activate)
            if self.layout:
                self.scanner.update_layer(self.layout, self.active_layer)
            else:
                _logger.warning("Failed to update scanner. No layout.")
        else:
            if self.scanner:
                self.scanner.finalize()
                self.scanner = None

    def _on_scanner_enabled(self, enabled):
        """ Config callback for scanner.enabled changes. """
        self.update_scanner_enabled()
        self.update_transparency()

    def _on_scanner_redraw(self, keys):
        """ Scanner callback for redraws. """
        self.redraw(keys)

    def _on_scanner_activate(self, key):
        """ Scanner callback for key activation. """
        self.key_down(key)
        self.key_up(key)

    def get_layers(self):
        if self.layout:
            return self.layout.get_layer_ids()
        return []

    def iter_keys(self, group_name=None):
        """ iterate through all keys or all keys of a group """
        if self.layout:
            return self.layout.iter_keys(group_name)
        else:
            return []

    def _on_mods_changed(self):
        self.invalidate_context_ui()
        self.commit_ui_updates()

    def get_pressed_key(self):
        return self._pressed_key

    def set_currently_typing(self):
        """ Remember it was us that just typed text. """
        self._last_typing_time = time.time()

    def is_typing(self):
        """ Is Onboard currently or was it just recently sending any text? """
        key = self.get_pressed_key()
        return key and self._is_text_insertion_key(key) or \
               time.time() - self._last_typing_time <= 0.3

    def _is_text_insertion_key(self, key):
        """ Does key actually insert any characters (not a navigation key)? """
        return key and \
               key.is_text_changing()

    def key_down(self, key, view=None, sequence=None, action=True):
        """
        Press down on one of Onboard's key representations.
        This may be either an initial press, or a switch of the active_key
        due to dragging.
        """
        self.on_any_key_down()
        self.stop_raise_attempts()

        if sequence:
            button = sequence.button
            event_type = sequence.event_type
        else:
            button = 1
            event_type = EventType.CLICK

        # Stop garbage collection delays until key release. They might cause
        # unexpected key repeats on slow systems.
        if gc.isenabled():
            gc.disable()

        if key and \
           key.sensitive:

            # stop timed redrawing for this key
            self._unpress_timers.stop(key)

            # announce temporary modifiers
            temp_mod_mask = 0
            if config.keyboard.can_upper_case_on_button(button):
                temp_mod_mask = Modifiers.SHIFT
            self._set_temporary_modifiers(temp_mod_mask)
            self._update_temporary_key_label(key, temp_mod_mask)

            # mark key pressed
            key.pressed = True
            self.on_key_pressed(key, view, sequence, action)

            # Get drawing behind us now, so it can't delay processing key_up()
            # and cause unwanted key repeats on slow systems.
            self.redraw([key])
            self.process_updates()

            # perform key action (not just dragging)?
            if action:
                self._do_key_down_action(key, view, button, event_type)

            # remember as pressed key
            if key not in self._pressed_keys:
                self._pressed_keys.append(key)

    def key_up(self, key, view=None, sequence=None, action=True):
        """ Release one of Onboard's key representations. """
        if sequence:
            button = sequence.button
            event_type = sequence.event_type
        else:
            button = 1
            event_type = EventType.CLICK

        if key and \
           key.sensitive:

            # Was the key nothing but pressed before?
            extend_pressed_state = key.is_pressed_only()

            # perform key action?
            # (not just dragging or canceled due to long press)
            if action:
                # If there was no down action yet (dragging), catch up now
                if not key.activated:
                    self._do_key_down_action(key, view, button, event_type)

                self._do_key_up_action(key, view, button, event_type)

                # Skip context and button controller updates for the common
                # letter press to improve responsiveness on slow systems.
                if key.type == KeyCommon.BUTTON_TYPE:
                    self.invalidate_context_ui()

            # no action but key was activated: must have been a long press
            elif key.activated:
                # switch to layer 0 after long pressing snippet buttons
                if key.type == KeyCommon.MACRO_TYPE:
                    self.maybe_switch_to_first_layer(key)

            # Is the key still nothing but pressed?
            extend_pressed_state = extend_pressed_state and \
                                   key.is_pressed_only() and \
                                   action

            # Draw key unpressed to remove the visual feedback.
            if extend_pressed_state and \
               not config.scanner.enabled:
                # Keep key pressed for a little longer for clear user feedback.
                self._unpress_timers.start(key)
            else:
                # Unpress now to avoid flickering of the
                # pressed color after key release.
                key.pressed = False
                self.on_key_unpressed(key)

            # no more actions left to finish
            key.activated = False

            # remove from list of pressed keys
            if key in self._pressed_keys:
                self._pressed_keys.remove(key)

            # Make note that it was us who just sent text
            # (vs. at-spi update due to scrolling, physical typing, ...).
            if self._is_text_insertion_key(key):
                self.set_currently_typing()

                # This key might have caused a completion popup to open,
                # e.g. the firefox URL-bar popup.
                # -> attempt to raise the keyboard over the popup
                # Disabled because only raising isn't enough for most
                # drop-downs.
                if False and \
                   action and \
                   config.is_force_to_top() and \
                   not self.has_focusable_gui() and \
                   not config.xid_mode:
                    self.raise_ui_delayed()

        # Was this the final touch sequence?
        if not self.has_input_sequences():
            self._non_modifier_released = False
            self._pressed_keys = []
            self._pressed_key = None
            self.on_all_keys_up()
            gc.enable()

        # Process pending UI updates
        self.commit_ui_updates()

    def key_long_press(self, key, view=None, button=1):
        """ Long press of one of Onboard's key representations. """
        long_pressed = False
        key_type = key.type

        if not config.xid_mode:
            # Is there a popup definition in the layout?
            sublayout = key.get_popup_layout()
            if sublayout:
                view.show_popup_layout(key, sublayout)
                long_pressed = True

            elif key_type == KeyCommon.BUTTON_TYPE:
                # Buttons decide for themselves what is to happen.
                controller = self.button_controllers.get(key)
                if controller:
                    controller.long_press(view, button)

            elif key.is_prediction_key():
                view.show_prediction_menu(key, button)
                long_pressed = True

            elif key_type == KeyCommon.MACRO_TYPE:
                snippet_id = int(key.code)
                self._edit_snippet(view, snippet_id)
                long_pressed = True
            else:
                # All other keys get hard-coded long press menus
                # (where available).
                action = self.get_key_action(key)
                if action == KeyCommon.DELAYED_STROKE_ACTION and \
                   not key.is_word_suggestion():
                    label = key.get_label()
                    alternatives = self.find_canonical_equivalents(label)
                    if alternatives:
                        self._touch_feedback.hide(key)
                        view.show_popup_alternative_chars(key, alternatives)
                    long_pressed = True

        if long_pressed:
            key.activated = True  # no more drag selection

        return long_pressed

    def _do_key_down_action(self, key, view, button, event_type):

        # generate key-stroke
        action = self.get_key_action(key)
        can_send_key = ((not key.sticky or not key.active) and
                        not action == KeyCommon.DELAYED_STROKE_ACTION)
        if can_send_key:
            self.send_key_down(key, view, button, event_type)

        # Modifier keys may change multiple keys
        # -> redraw all dependent keys
        # no danger of key repeats due to delays
        # -> redraw asynchronously
        if can_send_key and key.is_modifier():
            self.redraw_labels(False)

        if key.type == KeyCommon.BUTTON_TYPE:
            controller = self.button_controllers.get(key)
            if controller:
                key.activated = controller.is_activated_on_press()

    def _do_key_up_action(self, key, view, button, event_type):
        if key.sticky:
            # Multi-touch release?
            if key.is_modifier() and \
               not self._can_cycle_modifiers():
                can_send_key = True
            else:  # single touch/click
                can_send_key = self.step_sticky_key(key, button, event_type)

            if can_send_key:
                self.send_key_up(key, view)
                if key.is_modifier():
                    self.redraw_labels(False)
        else:
            self._release_non_sticky_key(key, view, button, event_type)

        # Multi-touch: temporarily stop cycling modifiers if
        # a non-modifier key was pressed. This way we get both,
        # cycling latched and locked state with single presses
        # and press-only action for multi-touch modifer + key press.
        if not key.is_modifier():
            self._non_modifier_released = True

    def send_key_down(self, key, view, button, event_type):
        if self.is_key_disabled(key):
            _logger.debug("send_key_down: "
                          "rejecting blacklisted key action for '{}'"
                          .format(key.id))
            return

        modifier = key.modifier

        if modifier == Modifiers.ALT and \
           self._is_alt_special():
            self._last_alt_key = key
        else:
            action = self.get_key_action(key)
            if action != KeyCommon.DELAYED_STROKE_ACTION:
                WordSuggestions.on_before_key_press(self, key)
                self._maybe_send_alt_press_for_key(key, view,
                                                   button, event_type)
                self._maybe_lock_temporary_modifiers(key)

                self.send_key_press(key, view, button, event_type)

            if action == KeyCommon.DOUBLE_STROKE_ACTION:  # e.g. CAPS
                self.send_key_release(key, view, button, event_type)

        if modifier:
            self._do_lock_modifier(modifier)

            # Update word suggestions on shift press.
            self.invalidate_context_ui()

            key.activated = True  # modifiers set -> can't undo press anymore

    def send_key_up(self, key, view = None, button = 1,
                    event_type = EventType.CLICK):
        if self.is_key_disabled(key):
            _logger.debug("send_key_up: "
                          "rejecting blacklisted key action for '{}'" \
                          .format(key.id))
            return

        key_type = key.type
        modifier = key.modifier
        action = self.get_key_action(key)

        # Unlock most modifiers before key release, otherwise Compiz wall
        # plugin's viewport switcher window doesn't close after
        # Alt+Ctrl+Up/Down (LP: #1532254).
        if modifier and \
           action != KeyCommon.DOUBLE_STROKE_ACTION: # not NumLock, CAPS
            self._do_unlock_modifier(modifier)

            # Update word suggestions on shift unlatch or release.
            self.invalidate_context_ui()

        # generate key event(s)
        if modifier == Modifiers.ALT and \
           self._is_alt_special():
            pass
        else:
            if action == KeyCommon.DOUBLE_STROKE_ACTION or \
               action == KeyCommon.DELAYED_STROKE_ACTION:

                WordSuggestions.on_before_key_press(self, key)
                self._maybe_send_alt_press_for_key(key, view,
                                                   button, event_type)
                self._maybe_lock_temporary_modifiers(key)

                if key_type == KeyCommon.CHAR_TYPE:
                    # allow  direct text insertion by AT-SPI for char keys
                    self._text_changer.insert_string_at_caret(key.code)
                else:
                    self.send_key_press(key, view, button, event_type)
                    self.send_key_release(key, view, button, event_type)
            else:
                self.send_key_release(key, view, button, event_type)

        # Unlock NumLock, CAPS, etc. after key events were sent,
        # else they are toggled right back on.
        if modifier and \
           action == KeyCommon.DOUBLE_STROKE_ACTION:
            self._do_unlock_modifier(modifier)

            # Update word suggestions on shift unlatch or release.
            self.invalidate_context_ui()

        self._maybe_unlock_temporary_modifiers()
        self._maybe_send_alt_release_for_key(key, view, button, event_type)

        # Check modifier counts for plausibility.
        # There might be a bug lurking that gets certain modifers stuck
        # with negative counts. Work around this and be verbose about it
        # so we can fix it eventually.
        # Seems fixed in 0.99, but keep the check just in case.
        # Happens again since 1.1.0 when using physical keyboards in
        # parallel with Onboard. Occasionally we fail to detect where a
        # modifier change originated from.
        for mod, nkeys in self._mods.items():
            if nkeys < 0:
                _logger.warning("Negative count {} for modifier {}, reset." \
                                .format(self.mods[modifier], modifier))
                self.mods[mod] = 0

                # Reset this too, else unlatching won't happen until restart.
                self._external_mod_changes[mod] = 0

    def _update_temporary_key_label(self, key, temp_mod_mask):
        """ update label for temporary modifiers """
        mod_mask = self.get_mod_mask()
        temp_mod_mask |= mod_mask
        if key.mod_mask != temp_mod_mask:
            key.configure_label(temp_mod_mask)

    def _set_temporary_modifiers(self, mod_mask):
        """ Announce the intention to lock these modifiers on key-press. """
        # only some single modifiers supported at this time
        if not mod_mask or \
           mod_mask in (Modifiers.SHIFT, Modifiers.CAPS, Modifiers.CTRL,
                        Modifiers.SUPER, Modifiers.ALTGR):
            self._temporary_modifiers = mod_mask

    def _maybe_lock_temporary_modifiers(self, key):
        """ Lock modifier before a single key-press """
        modifier = self._temporary_modifiers
        if modifier and \
           not self.mods[modifier] and \
           not key.modifier == modifier and \
           not key.is_button():
            self._locked_temporary_modifiers = modifier
            self._do_lock_modifier(modifier)

    def _maybe_unlock_temporary_modifiers(self):
        """ Unlock modifier after a single key-press """
        if self._locked_temporary_modifiers:
            self._do_unlock_modifier(self._locked_temporary_modifiers)
            self._locked_temporary_modifiers = None

    def _do_lock_modifier(self, modifier):
        """ Lock modifier in response to modifier presses. """
        # Increment this before lock_mod() to skip
        # updating keys a second time in set_modifiers().
        self.mods[modifier] += 1

        # Alt is special because it activates the window manager's move mode.
        if modifier != Modifiers.ALT or \
           not self._is_alt_special():  # not Alt?
            self._text_changer.lock_mod(modifier)

    def _do_unlock_modifier(self, modifier):
        """ Unlock modifier in response to modifier releases. """
        # Decrement before unlock_mod() to skip updating
        # keys a second time in set_modifiers().
        self.mods[modifier] -= 1

        # Alt is special because it activates window manager's move mode.
        if modifier != Modifiers.ALT or \
            not self._is_alt_special(): # not Alt?
            self._text_changer.unlock_mod(modifier)

    def _is_alt_special(self):
        """
        Does the ALT key need special treatment due to it
        """
        return not config.is_override_redirect()

    def _maybe_send_alt_press_for_key(self, key, view, button, event_type):
        """ handle delayed Alt press """
        if self.mods[8] and \
           self._is_alt_special() and \
           not key.active and \
           not self.is_key_disabled(key):
           not key.type == KeyCommon.BUTTON_TYPE and \
            self.maybe_send_alt_press(view, button, event_type)

    def _maybe_send_alt_release_for_key(self, key, view, button, event_type):
        """ handle delayed Alt release """
        if self._alt_locked:
            self.maybe_send_alt_release(view, button, event_type)

    def maybe_send_alt_press(self, view, button, event_type):
        if self.mods[8] and \
           not self._alt_locked:
            self._alt_locked = True
            if self._last_alt_key:
                self.send_key_press(self._last_alt_key, view, button, event_type)
            self._text_changer.lock_mod(8)

    def maybe_send_alt_release(self, view, button, event_type):
        if self._alt_locked:
            self._alt_locked = False
            if self._last_alt_key:
                self.send_key_release(self._last_alt_key, view, button, event_type)
            self._text_changer.unlock_mod(8)

    def send_key_press(self, key, view, button, event_type):
        """ Actually generate a fake key press """
        activated = True
        key_type = key.type

        if key_type == KeyCommon.KEYCODE_TYPE:
            with KeySynth.no_delay():
                self._text_changer.press_keycode(key.code)

        elif key_type == KeyCommon.KEYSYM_TYPE:
            with KeySynth.no_delay():
                self._text_changer.press_keysym(key.code)

        elif key_type == KeyCommon.CHAR_TYPE:
            if len(key.code) == 1:
                with KeySynth.no_delay():
                    self._text_changer.press_unicode(key.code)

        elif key_type == KeyCommon.KEYPRESS_NAME_TYPE:
            with KeySynth.no_delay():
                self._text_changer.press_keysym(get_keysym_from_name(key.code))

        elif key_type == KeyCommon.BUTTON_TYPE:
            activated = False
            controller = self.button_controllers.get(key)
            if controller:
                activated = controller.is_activated_on_press()
                controller.press(view, button, event_type)

        elif key_type == KeyCommon.MACRO_TYPE:
            activated = False

        elif key_type == KeyCommon.SCRIPT_TYPE:
            activated = False

        elif key_type == KeyCommon.WORD_TYPE:
            activated = False

        elif key_type == KeyCommon.CORRECTION_TYPE:
            activated = False

        key.activated = activated

    def send_key_release(self, key, view, button = 1, event_type = EventType.CLICK):
        """ Actually generate a fake key release """
        key_type = key.type
        if key_type == KeyCommon.CHAR_TYPE:
            if len(key.code) == 1:
                self._text_changer.release_unicode(key.code)
            else:
                self._text_changer.insert_string_at_caret(key.code)

        elif key_type == KeyCommon.KEYSYM_TYPE:
            self._text_changer.release_keysym(key.code)

        elif key_type == KeyCommon.KEYPRESS_NAME_TYPE:
            self._text_changer.release_keysym(get_keysym_from_name(key.code))

        elif key_type == KeyCommon.KEYCODE_TYPE:
            self._text_changer.release_keycode(key.code);

        elif key_type == KeyCommon.BUTTON_TYPE:
            controller = self.button_controllers.get(key)
            if controller:
                controller.release(view, button, event_type)

        elif key_type == KeyCommon.MACRO_TYPE:
            snippet_id = int(key.code)
            if self.insert_snippet(snippet_id):
                pass

            # Block dialog in xembed mode.
            # Don't allow to open multiple dialogs in force-to-top mode.
            else:
                self._edit_snippet(view, snippet_id)

        elif key_type == KeyCommon.SCRIPT_TYPE:
            if not config.xid_mode:  # block settings dialog in xembed mode
                if key.code:
                    run_script(key.code)

    def _edit_snippet(self, view, snippet_id):
        if not config.xid_mode and \
           not self.editing_snippet and \
           view:
            view.show_snippets_dialog(snippet_id)
            self.editing_snippet = True

    def insert_snippet(self, snippet_id):
        mlabel, mString = config.snippets.get(snippet_id, (None, None))
        if mString:
            self._text_changer.insert_string_at_caret(mString)
            return True
        return False

    def _release_non_sticky_key(self, key, view, button, event_type):
        # Request capitalization before keys are unlatched, so we can
        # prevent modifiers from toggling more than once and confuse
        # set_modifiers().
        WordSuggestions.on_before_key_release(self, key)

        # release key
        self.send_key_up(key, view, button, event_type)

        # Don't release latched modifiers for click buttons yet,
        # keep them unchanged until the actual click happens.
        # -> allow clicks with modifiers
        if not key.is_layer_button() and \
           not (key.type == KeyCommon.BUTTON_TYPE and
                key.is_click_type_key()) and \
           key not in self.get_text_displays():

            # Don't release SHIFT if we're going to enable
            # capitalization anyway.
            except_keys = None
            if self._capitalization_requested:
                except_keys = [key for key in self._latched_sticky_keys
                               if key.modifier == Modifiers.SHIFT]

            # release latched modifiers
            self.release_latched_sticky_keys(only_unpressed=True,
                                             except_keys=except_keys)

            # undo temporary suppression of the text display
            WordSuggestions.show_input_line_on_key_release(self, key)

        # Insert words on button release to avoid having the wordlist
        # change between button press and release.
        # Make sure latched modifiers have been released, else they will
        # affect the whole inserted string.
        WordSuggestions.send_key_up(self, key, button, event_type)

        # switch to layer 0 on (almost) any key release
        self.maybe_switch_to_first_layer(key)

        # punctuation assistance and collapse corrections
        WordSuggestions.on_after_key_release(self, key)

        # capitalization requested by punctuator?
        if self._capitalization_requested:
            self._capitalization_requested = False
            if not self.mods[Modifiers.SHIFT]:  # SHIFT not active yet?
                self._enter_caps_mode()

    def request_capitalization(self, capitalize):
        """
        Request entering upper-caps mode after next key-release.
        """
        self._capitalization_requested = capitalize

    def _enter_caps_mode(self):
        """
        Do what has to be done so that the next pressed
        character will be capitalized.

        Don't call key_down+up for this, because modifiers may be
        configured not to latch.
        """
        lfsh_keys = self.find_items_from_ids(["LFSH"])
        rtsh_keys = self.find_items_from_ids(["RTSH"])

        # unlatch all shift keys
        for key in rtsh_keys + lfsh_keys:
            if key.active:
                key.active = False
                key.locked = False
                if key in self._latched_sticky_keys:
                    self._latched_sticky_keys.remove(key)
                if key in self._locked_sticky_keys:
                    self._locked_sticky_keys.remove(key)
            self.redraw([key])

        # Latch right shift for capitalization,
        # if there is no right shift latch left shift instead.
        shift_keys = rtsh_keys if rtsh_keys else lfsh_keys
        for key in shift_keys:
            if not key.active:
                key.active = True
                if key not in self._latched_sticky_keys:
                    self._latched_sticky_keys.append(key)
                self.redraw([key])

        self.mods[Modifiers.SHIFT] = 1
        self._text_changer.lock_mod(1)
        self.redraw_labels(False)

    def maybe_switch_to_first_layer(self, key):
        """
        Activate the first layer if key allows it.
        """
        if self.active_layer_index != 0 and \
           not self._layer_locked:

            unlatch = key.can_unlatch_layer()
            if unlatch is None:
                # for backwards compatibility with Onboard <0.99
                unlatch = not key.is_layer_button() and \
                          not key.id in ["move", "showclick"]

            if unlatch:
                self.active_layer_index = 0
                self.invalidate_visible_layers()
                self.invalidate_canvas()
                self.invalidate_context_ui()  # update layer button state

            return unlatch

    def update_modifiers(self):
        """
        Synchronize our keys with externally activated modifiers,
        e.g. by physical keyboards or tools like xte.
        """
        keymap = Gdk.Keymap.get_default()
        if keymap:
            mod_mask = keymap.get_modifier_state()
            self.set_modifiers(mod_mask)

    def set_modifiers(self, mod_mask):
        """
        Sync Onboard with modifiers of the given modifier mask.
        Used to sync changes of system modifier state with Onboard.
        """
        # The special handling of ALT in Onboard confuses the detection of
        # modifier presses from the outside.
        # Test case: press ALT, then LSHIFT
        # Expected:  LSHIFT latched
        # Result:    LSHIFT locked and RSHIFT latched
        # -> stop all modifier synchronization while the ALT key is active.
        if self._alt_locked:
            return

        if self._temporary_modifiers:
            return

        # SHIFT doesn't unlatch in Firefox, launchpad question entry, typing
        # "?" after inserting "collection" with Small layout, Xenial,
        if self.is_typing():
            return

        for mod_bit in (1 << bit for bit in range(8)):
            # Directly redraw locking modifiers only. All other modifiers
            # redraw after a short delay. This is meant to prevent
            # Onboard from busily flashing keys and using CPU while
            # typing with a hardware keyboard.
            if (mod_bit & (Modifiers.CAPS | Modifiers.NUMLK)):
                delay = 0
            else:
                delay = config.keyboard.modifier_update_delay

            # -1.0 restores the onboard 1.0.0 behavior, no updates
            if delay >= 0:
                self.set_modifier(mod_bit, bool(mod_mask & mod_bit), delay)

    def set_modifier(self, mod_bit, active, draw_delay=0.0):
        """
        Update Onboard to reflect the state of the given modifier in the ui.
        """
        # find all keys assigned to the modifier bit
        keys = []
        for key in self.layout.iter_keys():
            if key.modifier == mod_bit:
                keys.append(key)

        active_before = bool(self._mods[mod_bit])

        # Was modifier turned on?
        if not active_before and active:
            self._mods[mod_bit] += 1
            self._external_mod_changes[mod_bit] = 1
            for key in keys:
                if key.sticky:
                    self.step_sticky_key(key, 1, EventType.CLICK)

        # Was modifier turned off?
        elif active_before and not active:
            self._mods[mod_bit] = 0
            self._external_mod_changes[mod_bit] = 0
            for key in keys:
                if key in self._latched_sticky_keys:
                    self._latched_sticky_keys.remove(key)
                if key in self._locked_sticky_keys:
                    self._locked_sticky_keys.remove(key)
                key.active = False
                key.locked = False

        # Was it a change from the outside, i.e. not us?
        # For this to work, we always have to update self._mods _before_
        # lock_mod calls when our modifier keys are clicked.
        if active != active_before:

            # re-draw delayed?
            if active and \
               draw_delay > 0.0:
                self._queue_pending_modifier_redraw(mod_bit, active, keys, draw_delay)
            else:
                self._redraw_modifier_keys(keys)

    def _queue_pending_modifier_redraw(self, mod_bit, active, keys, delay):
        item = self._pending_modifier_redraws.get(mod_bit)
        if item is None:
            # draw affected keys delayed
            self._pending_modifier_redraws[mod_bit] = (active, keys)
        else:
            pending_active, keys = item

            # discard redraw if the modifier change didn't have
            # any lasting effects
            if active != pending_active:
                del self._pending_modifier_redraws[mod_bit]

        # start/restart/stop timer
        if self._pending_modifier_redraws:
            self._pending_modifier_redraws_timer.start(
                delay, self._redraw_pending_modifier_keys)
        else:
            self._pending_modifier_redraws_timer.stop()

    def _redraw_pending_modifier_keys(self):
        for pending_active, keys in self._pending_modifier_redraws.values():
            self._redraw_modifier_keys(keys)
        self._pending_modifier_redraws = {}

    def _redraw_modifier_keys(self, keys):
        # redraw modifier keys
        self.redraw(keys)
        # redraw keys where labels are affected by the modifier change
        self.redraw_labels(False)

    def step_sticky_key(self, key, button, event_type):
        """
        One cycle step when pressing a sticky (latchabe/lockable)
        modifier key (all sticky keys except layer buttons).
        """
        active, locked = self.step_sticky_key_state(key,
                                                    key.active, key.locked,
                                                    button, event_type)
        # apply the new states
        was_active  = key.active
        deactivated = False
        key.active  = active
        key.locked  = locked
        if active:
            if locked:
                if key in self._latched_sticky_keys:
                    self._latched_sticky_keys.remove(key)
                if not key in self._locked_sticky_keys:
                    self._locked_sticky_keys.append(key)
            else:
                if not key in self._latched_sticky_keys:
                    self._latched_sticky_keys.append(key)
                if key in self._locked_sticky_keys:
                    self._locked_sticky_keys.remove(key)
        else:
            if key in self._latched_sticky_keys:
                self._latched_sticky_keys.remove(key)
            if key in self._locked_sticky_keys:
                self._locked_sticky_keys.remove(key)

            deactivated = was_active or \
                          not self.can_activate_key(key) # push-button

        return deactivated

    def can_activate_key(self, key):
        """ Can key be latched or locked? """
        behavior = self._get_sticky_key_behavior(key)
        return StickyBehavior.can_latch(behavior) or \
               StickyBehavior.can_lock(behavior)

    def step_sticky_key_state(self, key, active, locked, button, event_type):
        """ One cycle step when pressing a sticky (latchabe/lockable) key """
        behavior = self._get_sticky_key_behavior(key)
        double_click = event_type == EventType.DOUBLE_CLICK

        # double click usable?
        if double_click and \
           StickyBehavior.can_lock_on_double_click(behavior):

            # any state -> locked
            active = True
            locked = True

        # single click or unused double click
        else:
            # off -> latched or locked
            if not active:

                if StickyBehavior.can_latch(behavior):
                    active = True

                elif StickyBehavior.can_lock_on_single_click(behavior):
                    active = True
                    locked = True

            # latched -> locked
            elif not key.locked and \
                 StickyBehavior.can_lock_on_single_click(behavior):
                locked = True

            # latched or locked -> off
            elif StickyBehavior.can_cycle(behavior):
                active = False
                locked = False

        return active, locked

    def _get_sticky_key_behavior(self, key):
        """ Return sticky behavior for the given key """
        # try the individual key id
        behavior = self._get_sticky_behavior_for(key.id)

        # default to the layout's behavior
        # CAPS was hard-coded here to LOCK_ONLY until v0.98.
        if behavior is None and \
           not key.sticky_behavior is None:
            behavior = key.sticky_behavior

        # try the key group
        if behavior is None:
            if key.is_modifier():
                behavior = self._get_sticky_behavior_for("modifiers")
            if key.is_layer_button():
                behavior = self._get_sticky_behavior_for("layers")

        # try the 'all' group
        if behavior is None:
            behavior = self._get_sticky_behavior_for("all")

        # else fall back to hard coded default
        if not StickyBehavior.is_valid(behavior):
            behavior = StickyBehavior.CYCLE

        return behavior

    def _get_sticky_behavior_for(self, group):
        behavior = None
        value = config.keyboard.sticky_key_behavior.get(group)
        if value:
            try:
                behavior = StickyBehavior.from_string(value)
            except KeyError:
                _logger.warning("Invalid sticky behavior '{}' for group '{}'" \
                                .format(value, group))
        return behavior

    def on_snippets_dialog_closed(self):
        self.editing_snippet = False

    def is_key_disabled(self, key):
        """ Check for blacklisted key combinations """
        if self._disabled_keys is None:
            self._disabled_keys = self.create_disabled_keys_set()
            _logger.debug("disabled keys: {}".format(repr(self._disabled_keys)))

        set_key = (key.id, self.get_mod_mask())
        return set_key in self._disabled_keys

    def create_disabled_keys_set(self):
        """
        Precompute a set of (modmask, key_id) tuples for fast
        testing against the key blacklist.
        """
        disabled_keys = set()
        available_key_ids = [key.id for key in self.layout.iter_keys()]
        for combo in config.lockdown.disable_keys:
            results = parse_key_combination(combo, available_key_ids)
            if not results is None:
                disabled_keys.update(results)
            else:
                _logger.warning("ignoring unrecognized key combination '{}' in "
                                "lockdown.disable-keys" \
                                .format(key_str))
        return disabled_keys

    def get_key_action(self, key):
        action = key.action
        if action is None:
            if key.type == KeyCommon.BUTTON_TYPE:
                action = KeyCommon.DELAYED_STROKE_ACTION
                controller = self.button_controllers.get(key)
                if controller and \
                   controller.is_activated_on_press():
                    action = KeyCommon.SINGLE_STROKE_ACTION

            elif (key.type != KeyCommon.WORD_TYPE and
                  key.type != KeyCommon.CORRECTION_TYPE):
                label = key.get_label()
                alternatives = self.find_canonical_equivalents(label)
                if (len(label) == 1 and label.isalnum()) or \
                   key.id == "SPCE" or \
                   bool(alternatives):
                    action = config.keyboard.default_key_action
                else:
                    action = KeyCommon.SINGLE_STROKE_ACTION

        # Is there a popup defined for this key?
        if action != KeyCommon.DELAYED_STROKE_ACTION and \
           key.get_popup_layout():
            action = KeyCommon.DELAYED_STROKE_ACTION

        return action

    def has_latched_sticky_keys(self, except_keys=None):
        """ any sticky keys latched? """
        return len(self._latched_sticky_keys) > 0

    def release_latched_sticky_keys(self, except_keys = None,
                                    only_unpressed = False,
                                    skip_externally_set_modifiers = True):
        """ release latched sticky (modifier) keys """
        if len(self._latched_sticky_keys) > 0:
            for key in self._latched_sticky_keys[:]:
                if not except_keys or not key in except_keys:

                    # Don't release still pressed modifiers, they may be
                    # part of a multi-touch key combination.
                    if not only_unpressed or not key.pressed:

                        # Don't release modifiers that where latched by
                        # set_modifiers due to external (physical keyboard)
                        # action.
                        # Else the latched modifiers go out of sync in
                        # on_outside_click() while an external tool like
                        # xte holds them down (LP: #1331549).
                        if not skip_externally_set_modifiers or \
                           not key.is_modifier() or \
                           not self._external_mod_changes[key.modifier]:

                            # Keep shift pressed if we're going to continue
                            # upper-case anyway. Else multiple locks and
                            # unlocks of SHIFT may happen when the punctuator
                            # is active. We can change the modifier state at
                            # most once per key release, else we can't
                            # distinguish our changes from physical
                            # keyboard actions in set_modifiers.
                            if not key.modifier == Modifiers.SHIFT or \
                               not self._capitalization_requested:

                                self.send_key_up(key)
                                self._latched_sticky_keys.remove(key)
                                key.active = False
                                self.redraw([key])

            # modifiers may change many key labels -> redraw everything
            self.redraw_labels(False)

    def release_locked_sticky_keys(self, release_all = False):
        """ release locked sticky (modifier) keys """
        if len(self._locked_sticky_keys) > 0:
            for key in self._locked_sticky_keys[:]:
                # NumLock is special, keep its state on exit
                # if not told otherwise.
                if release_all or \
                   not key.modifier == Modifiers.NUMLK:
                    self.send_key_up(key)
                    self._locked_sticky_keys.remove(key)
                    key.active = False
                    key.locked = False
                    key.pressed = False
                    self.redraw([key])

            # modifiers may change many key labels -> redraw everything
            self.redraw_labels(False)

    def _can_cycle_modifiers(self):
        """
        Modifier cycling enabled?
        Not enabled for multi-touch with at least one pressed non-modifier key.
        """
        # Any non-modifier currently held down?
        for key in self._pressed_keys:
            if not key.is_modifier():
                return False

        # Any non-modifier released before?
        if self._non_modifier_released:
            return False

        return True

    def find_canonical_equivalents(self, char):
        return canonical_equivalents["all"].get(char)

    def invalidate_ui(self):
        """
        Update everything.
        Quite expensive, don't call this while typing.
        """
        self._invalidated_ui |= UIMask.ALL

    def invalidate_ui_no_resize(self):
        """
        Update everything assuming key sizes don't change.
        Doesn't invalidate cached surfaces.
        """
        self._invalidated_ui |= UIMask.ALL & ~UIMask.SIZE

    def invalidate_context_ui(self):
        """ Update text-context dependent ui """
        self._invalidated_ui |= UIMask.CONTROLLERS | \
                                UIMask.SUGGESTIONS | \
                                UIMask.LAYOUT

    def invalidate_layout(self):
        """
        Recalculate item rectangles.
        """
        self._invalidated_ui |= UIMask.LAYOUT

    def invalidate_visible_layers(self):
        """
        Update visibility of layers in the layout tree,
        e.g. when the active layer changed.
        """
        self._invalidated_ui |= UIMask.LAYERS

    def invalidate_canvas(self):
        """ Just redraw everything """
        self._invalidated_ui |= UIMask.REDRAW

    def commit_ui_updates(self):
        keys = set()
        mask = self._invalidated_ui

        if mask & UIMask.CONTROLLERS:
            # update buttons
            for controller in self.button_controllers.values():
                controller.update()
            mask = self._invalidated_ui  # may have been changed by controllers

        if mask & UIMask.SUGGESTIONS:
            keys.update(WordSuggestions.update_suggestions_ui(self))

            # update buttons that depend on suggestions
            for controller in self.button_controllers.values():
                controller.update_late()

        if mask & UIMask.LAYERS:
            self.update_visible_layers()

        if mask & UIMask.LAYOUT:
            self.update_layout()   # after suggestions!

        if mask & (UIMask.SUGGESTIONS | UIMask.LAYERS):
            self.update_scanner()

        for view in self._layout_views:
            view.apply_ui_updates(mask)

        if mask & UIMask.REDRAW:
            self.redraw()
        elif keys:
            self.redraw(list(keys))

        self._invalidated_ui = 0

    def update_layout(self):
        """
        Update layout, key sizes are probably changing.
        """
        for view in self._layout_views:
            view.update_layout()

    def update_visible_layers(self):
        """ show/hide layers """
        layout = self.layout
        if layout:
            layers = layout.get_layer_ids()
            if layers:
                layout.set_visible_layers([layers[0], self.active_layer])

    def update_scanner(self):
        """ tell scanner to update on layout changes """
        # notify the scanner about layer changes
        if self.scanner:
            layout = self.layout
            if layout:
                self.scanner.update_layer(layout, self.active_layer, True)
            else:
                _logger.warning("Failed to update scanner. No layout.")

    def hide_touch_feedback(self):
        self._touch_feedback.hide()

    def on_key_pressed(self, key, view, sequence, action):
        """ pressed state of a key instance was set """
        if sequence: # Not a simulated key press, scanner?
            feedback = self.can_give_keypress_feedback()

            # audio feedback
            if action and \
               config.keyboard.audio_feedback_enabled:
                pt = sequence.root_point \
                     if feedback else (-1, -1) # keep passwords privat
                pts = pt \
                      if config.keyboard.audio_feedback_place_in_space \
                      else (-1, -1)
                Sound().play(Sound.key_feedback, pt[0], pt[1], pts[0], pts[1])

            # key label popup
            if not config.xid_mode and \
               config.keyboard.touch_feedback_enabled and \
               sequence.event_type != EventType.DWELL and \
               key.can_show_label_popup() and \
               feedback:
                self._touch_feedback.show(key, view)

    def on_key_unpressed(self, key):
        """ pressed state of a key instance was cleard """
        self._set_temporary_modifiers(0)
        self._update_temporary_key_label(key, 0)
        self.redraw([key])
        self._touch_feedback.hide(key)

    def on_outside_click(self, button):
        """
        Called by outside click polling.
        Keep this as Francesco likes to have modifiers
        reset when clicking outside of onboard.
        """
        self.release_latched_sticky_keys()
        self._click_sim.end_mapped_click()
        WordSuggestions.on_outside_click(self, button)

    def on_cancel_outside_click(self):
        """ Called when outside click polling times out. """
        WordSuggestions.on_cancel_outside_click(self)

    def get_click_simulator(self):
        if config.mousetweaks and \
           config.mousetweaks.is_active():
            return config.mousetweaks
        return self._click_sim

    def ignore_capslock(self):
        """ Keep capslock from causing another send_key_up call on exit """
        for key in self.iter_keys():
            if key.modifier == Modifiers.CAPS:
                key.pressed = False
                key.active = False
                key.locked = False
                if key in self._latched_sticky_keys:
                    self._latched_sticky_keys.remove(key)
                if key in self._locked_sticky_keys:
                    self._locked_sticky_keys.remove(key)

    def release_pressed_keys(self, redraw = False):
        """
        Release pressed keys on exit, or when recreating the main window.
        """
        self.hide_touch_feedback()

        # Clear key.pressed for all keys that have already been released
        # but are still waiting for redrawing the unpressed state.
        self._unpress_timers.cancel_all()

        # Release keys that haven't been released yet
        for key in self.iter_keys():
            if key.pressed and key.type in \
                [KeyCommon.CHAR_TYPE,
                 KeyCommon.KEYSYM_TYPE,
                 KeyCommon.KEYPRESS_NAME_TYPE,
                 KeyCommon.KEYCODE_TYPE]:

                # Release still pressed enter key when onboard gets killed
                # on enter key press.
                _logger.warning("Releasing still pressed key '{}'" \
                              .format(key.id))
                self.send_key_up(key)
                key.pressed = False

                if redraw:
                    self.redraw([key])

    def update_auto_show(self):
        """
        Turn on/off auto-show in response to user action (preferences)
        and show/hide the views accordingly.
        """
        enable = config.is_auto_show_enabled()
        self._auto_show.enable(enable)
        self._auto_show.show_keyboard(not enable)
        self.update_auto_hide()

    def update_auto_hide(self):
        enabled_before = self._auto_hide.is_enabled()
        enabled_after = config.is_auto_hide_enabled()

        self._auto_hide.enable(enabled_after)

        if enabled_before and not enabled_after:
            self.resume_auto_show()

    def update_auto_show_on_visibility_change(self, visible):
        if config.is_auto_show_enabled():
            if visible and self._auto_show.is_paused():
                self.lock_auto_show_visible(False)
                self.resume_auto_show()
            else:
                self.lock_auto_show_visible(visible)

    def lock_auto_show_visible(self, visible):
        """
        If the user unhides onboard, don't auto-hide it until
        he manually hides it again.
        """
        if config.is_auto_show_enabled():
            self._auto_show.lock_visible(visible)

    def is_auto_show_paused(self):
        return self._auto_show.is_paused()

    def pause_auto_show(self, duration = None):
        """
        Stop both, hiding and showing long term.
        """
        if config.is_auto_show_enabled():
            self._auto_show.pause(duration)

    def resume_auto_show(self):
        """
        Reenable both, hiding and showing.
        """
        if config.is_auto_show_enabled():
            self._auto_show.resume()

    def freeze_auto_show(self, thaw_time = None):
        """
        Stop both, hiding and showing short term.
        """
        if config.is_auto_show_enabled():
            self._auto_show.freeze(thaw_time)

    def thaw_auto_show(self, thaw_time = None):
        """
        Reenable both, hiding and showing.
        """
        if config.is_auto_show_enabled():
            self._auto_show.thaw(thaw_time)

    def auto_position(self):
        self._broadcast_to_views("auto_position")

    def stop_auto_positioning(self):
        self._broadcast_to_views("stop_auto_positioning")

    def get_auto_show_repositioned_window_rect(self, view, home, limit_rects,
                                           test_clearance, move_clearance,
                                           horizontal = True, vertical = True):
        return self._auto_show.get_repositioned_window_rect(
                                           view, home, limit_rects,
                                           test_clearance, move_clearance,
                                           horizontal, vertical)

    def is_visible(self):
        return self._broadcast_to_first_view("is_visible", default=False)

    def transition_visible_to(self, show):
        return self._broadcast_to_views("transition_visible_to", show)

    def commit_transition(self):
        return self._broadcast_to_views("commit_transition")

    def raise_ui_delayed(self):
        """
        Attempt to raise keyboard over popups like the one from the firefox
        URL bar. Give it a moment for the popup to appear after a keypress.
        """
        self._raise_timer.growth = 2.0
        self._raise_timer.max_duration = 2.0
        self._raise_timer.start(0.1, self._on_raise_timer)

    def _on_raise_timer(self):
        _logger.warning("raising window - current delay {}s" \
                        .format(self._raise_timer._current_delay))
        self._broadcast_to_views("raise_to_top")
        self._touch_feedback.raise_all()
        return True

    def stop_raise_attempts(self):
        self._raise_timer.stop()

    def _broadcast_to_views(self, func_name, *params):
        for view in self._layout_views:
            if hasattr(view, func_name):
                getattr(view, func_name)(*params)

    def _broadcast_to_first_view(self, func_name, default, *params):
        for view in self._layout_views:
            if hasattr(view, func_name):
                return getattr(view, func_name)(*params)
        return default

    def find_items_from_ids(self, ids):
        if self.layout is None:
            return []
        return list(self.layout.find_ids(ids))

    def find_items_from_classes(self, item_classes):
        if self.layout is None:
            return []
        return list(self.layout.find_classes(item_classes))

    def find_key_from_id(self, id):
        """
        Find the first key matching the given id. Id may be a complete
        theme_id (key.theme_id) or just the regular item id (key.id).
        """
        for key in self.iter_keys():
            if key.theme_id:
                if key.theme_id == id:
                    return key
                elif key.id == id:
                    return key
        return None


class ButtonController(object):
    """
    MVC inspired controller that handles events and the resulting
    state changes of buttons.
    """
    def __init__(self, keyboard, key):
        self.keyboard = keyboard
        self.key = key

    def press(self, view, button, event_type):
        """ button pressed """
        pass

    def long_press(self, view, button):
        """ button pressed long """
        pass

    def release(self, view, button, event_type):
        """ button released """
        pass

    def update(self):
        """ asynchronous ui update """
        pass

    def update_late(self):
        """ after suggestions have been updated """
        pass

    def can_dwell(self):
        """ can start dwelling? """
        return False

    def is_activated_on_press(self):
        """ Cannot cancel already called press() without consequences? """
        return False

    def set_visible(self, visible):
        if self.key.visible != visible:
            _logger.debug("ButtonController: {}.visible = {}" \
                          .format(self.key, visible))
            layout = self.keyboard.layout
            layout.set_item_visible(self.key, visible)
            self.keyboard.redraw([self.key])

    def set_sensitive(self, sensitive):
        if self.key.sensitive != sensitive:
            _logger.debug("ButtonController: {}.sensitive = {}" \
                          .format(self.key, sensitive))
            self.key.sensitive = sensitive
            self.keyboard.redraw([self.key])

    def set_active(self, active = None):
        if not active is None and self.key.active != active:
            _logger.debug("ButtonController: {}.active = {}" \
                          .format(self.key, active))
            self.key.active = active
            self.keyboard.redraw([self.key])

    def set_locked(self, locked = None):
        if not locked is None and self.key.locked != locked:
            _logger.debug("ButtonController: {}.locked = {}" \
                          .format(self.key, locked))
            self.key.active = locked
            self.key.locked = locked
            self.keyboard.redraw([self.key])


class BCClick(ButtonController):
    """ Controller for click buttons """
    def release(self, view, button, event_type):
        cs = self.keyboard.get_click_simulator()
        if not cs:
            return
        if self.is_active():
            # stop click mapping, reset to primary button and single click
            cs.map_primary_click(view, ClickSimulator.PRIMARY_BUTTON,
                                       ClickSimulator.CLICK_TYPE_SINGLE)
        else:
            # Exclude click type buttons from the click mapping
            # to be able to reliably cancel the click.
            # -> They will receive only single left clicks.
            rects = view.get_click_type_button_rects()
            self.keyboard._click_sim.set_exclusion_rects(rects)

            # start click mapping
            cs.map_primary_click(view, self.button, self.click_type)

        # Mark current event handled to stop ClickMapper from receiving it.
        view.set_xi_event_handled(True)

    def update(self):
        cs = self.keyboard.get_click_simulator()
        if cs:  # gone on exit
            self.set_active(self.is_active())
            self.set_sensitive(
                cs.supports_click_params(self.button, self.click_type))

    def is_active(self):
        cs = self.keyboard.get_click_simulator()
        return cs and \
               cs.get_click_button() == self.button and \
               cs.get_click_type() == self.click_type

class BCSingleClick(BCClick):
    id = "singleclick"
    button = ClickSimulator.PRIMARY_BUTTON
    click_type = ClickSimulator.CLICK_TYPE_SINGLE

class BCMiddleClick(BCClick):
    id = "middleclick"
    button = ClickSimulator.MIDDLE_BUTTON
    click_type = ClickSimulator.CLICK_TYPE_SINGLE

class BCSecondaryClick(BCClick):
    id = "secondaryclick"
    button = ClickSimulator.SECONDARY_BUTTON
    click_type = ClickSimulator.CLICK_TYPE_SINGLE

class BCDoubleClick(BCClick):
    id = "doubleclick"
    button = ClickSimulator.PRIMARY_BUTTON
    click_type = ClickSimulator.CLICK_TYPE_DOUBLE

class BCDragClick(BCClick):
    id = "dragclick"
    button = ClickSimulator.PRIMARY_BUTTON
    click_type = ClickSimulator.CLICK_TYPE_DRAG

    def release(self, view, button, event_type):
        BCClick.release(self, view, button, event_type)
        self.keyboard.show_touch_handles(show = self._can_show_handles(),
                                         auto_hide = False)

    def update(self):
        active_before = self.key.active
        BCClick.update(self)
        active_now = self.key.active

        if active_before and not active_now:
            # hide the touch handles
            self.keyboard.show_touch_handles(self._can_show_handles())

    def _can_show_handles(self):
        return self.is_active() and \
               config.is_mousetweaks_active() and \
               not config.xid_mode


class BCHoverClick(ButtonController):

    id = "hoverclick"

    def release(self, view, button, event_type):
        config.enable_hover_click(not config.mousetweaks.is_active())

    def update(self):
        available = bool(config.mousetweaks)
        active    = config.mousetweaks.is_active() \
                    if available else False

        self.set_sensitive(available and \
                           not config.lockdown.disable_hover_click)
        # force locked color for better visibility
        self.set_locked(active)
        #self.set_active(config.mousetweaks.is_active())

    def can_dwell(self):
        return not (config.mousetweaks and config.mousetweaks.is_active())


class BCHide(ButtonController):

    id = "hide"

    def release(self, view, button, event_type):
        if config.unity_greeter:
            config.unity_greeter.onscreen_keyboard = False
        else:
            self.keyboard.set_visible(False)

    def update(self):
        # insensitive in XEmbed mode except in unity-greeter
        self.set_sensitive(not config.xid_mode or \
                           config.unity_greeter)


class BCShowClick(ButtonController):

    id = "showclick"

    def release(self, view, button, event_type):
        config.keyboard.show_click_buttons = not config.keyboard.show_click_buttons

        # enable hover click when the key was dwell-activated
        # disabled for now, seems too confusing
        if False:
            if event_type == EventType.DWELL and \
               config.keyboard.show_click_buttons and \
               not config.mousetweaks.is_active():
                config.enable_hover_click(True)

    def update(self):
        allowed = not config.lockdown.disable_click_buttons

        self.set_visible(allowed)

        # Don't show active state. Toggling the click column
        # should be enough feedback.
        #self.set_active(config.keyboard.show_click_buttons)

        # show/hide click buttons
        show_click = config.keyboard.show_click_buttons and allowed
        layout = self.keyboard.layout
        if layout:
            for item in layout.iter_items():
                if item.group == 'click':
                    layout.set_item_visible(item, show_click)
                elif item.group == 'noclick':
                    layout.set_item_visible(item, not show_click)

    def can_dwell(self):
        return not config.mousetweaks or not config.mousetweaks.is_active()


class BCMove(ButtonController):

    id = "move"

    def press(self, view, button, event_type):
        if not config.xid_mode:
            # not called from popup?
            if hasattr(view, "start_move_window"):
                view.start_move_window()

    def long_press(self, view, button):
        if not config.xid_mode:
            self.keyboard.show_touch_handles(True)

    def release(self, view, button, event_type):
        if not config.xid_mode:
            if hasattr(view, "start_move_window"):
                view.stop_move_window()
            else:
                # pressed in a popup just show touch handles
                self.keyboard.show_touch_handles(True)

    def update(self):
        self.set_visible(not config.has_window_decoration() and \
                         not config.xid_mode and \
                         Handle.MOVE in config.window.window_handles)

    def is_activated_on_press(self):
        return True  # cannot undo on press, dragging is already in progress


class BCLayer(ButtonController):
    """ layer switch button, switches to layer <layer_index> when released """

    layer_index = None

    def _get_id(self):
        return "layer" + str(self.layer_index)
    id = property(_get_id)

    def release(self, view, button, event_type):
        keyboard = self.keyboard

        active_before = keyboard.active_layer_index == self.layer_index
        locked_before = active_before and keyboard._layer_locked

        active, locked = keyboard.step_sticky_key_state(
                                       self.key,
                                       active_before, locked_before,
                                       button, event_type)

        # push buttons switch layers even though they don't activate the key
        if not keyboard.can_activate_key(self.key):
            active = True

        keyboard.active_layer_index = self.layer_index \
                                      if active else 0

        keyboard._layer_locked       = locked \
                                      if self.layer_index else False

        if active_before != active:
            keyboard.invalidate_visible_layers()
            keyboard.invalidate_canvas()

    def update(self):
        # don't show active state for layer 0, it'd be visible all the time
        active = self.key.show_active and \
                 self.key.get_layer_index() == self.keyboard.active_layer_index
        if active:
            active = self.keyboard.can_activate_key(self.key)

        self.set_active(active)
        self.set_locked(active and self.keyboard._layer_locked)


class BCPreferences(ButtonController):

    id = "settings"

    def release(self, view, button, event_type):
        run_script("sokSettings")

    def update(self):
        self.set_visible(not config.xid_mode and \
                         not config.running_under_gdm and \
                         not config.lockdown.disable_preferences)


class BCQuit(ButtonController):

    id = "quit"

    def release(self, view, button, event_type):
        app = self.keyboard.get_application()
        if app:
            # finish current key processing then quit
            GLib.idle_add(app.do_quit_onboard)

    def update(self):
        self.set_visible(not config.xid_mode and
                         not config.lockdown.disable_quit)


class BCExpandCorrections(ButtonController):

    id = "expand-corrections"

    def release(self, view, button, event_type):
        wordlist = self.key.get_parent()
        wordlist.expand_corrections(not wordlist.are_corrections_expanded())


class BCPreviousPredictions(ButtonController):

    id = "previous-predictions"

    def release(self, view, button, event_type):
        wordlist = self.key.get_parent()
        wordlist.goto_previous_predictions()
        self.keyboard.invalidate_context_ui()

    def update_late(self):
        wordlist = self.key.get_parent()
        self.set_sensitive(wordlist.can_goto_previous_predictions())


class BCNextPredictions(ButtonController):

    id = "next-predictions"

    def release(self, view, button, event_type):
        wordlist = self.key.get_parent()
        wordlist.goto_next_predictions()
        self.keyboard.invalidate_context_ui()

    def update_late(self):
        key = self.key
        wordlist = key.get_parent()
        self.set_sensitive(wordlist.can_goto_next_predictions())


class BCPauseLearning(ButtonController):

    id = "pause-learning"

    def release(self, view, button, event_type):
        keyboard = self.keyboard
        key = self.key

        active, locked = keyboard.step_sticky_key_state(key,
                                                        key.active, key.locked,
                                                        button, event_type)
        key.active  = active
        key.locked  = locked

        value = 0
        if active:
            value += 1
        if locked:
            value += 1

        pause_started = config.word_suggestions.get_pause_learning() == 0 and \
                        value > 0

        config.word_suggestions.set_pause_learning(value)

        # immediately forget changes
        if pause_started:
            keyboard.discard_changes()

    def update(self):
        co = config.word_suggestions
        self.set_active(co.get_pause_learning() >= 1)
        self.set_locked(co.get_pause_learning() == 2)


class BCLanguage(ButtonController):

    id = "language"

    def __init__(self, keyboard, key):
        ButtonController.__init__(self, keyboard, key)
        self._menu_close_time = 0

    def release(self, view, button, event_type):
        if time.time() - self._menu_close_time > 0.5:
            self.set_active(not self.key.active)
            if self.key.active:
                self._show_menu(view, self.key, button)
        self._menu_close_time = 0

    def _show_menu(self, view, key, button):
        self.keyboard.hide_touch_feedback()
        view.show_language_menu(key, button, self._on_menu_closed)

    def _on_menu_closed(self):
        self.set_active(False)
        self._menu_close_time = time.time()

    def update(self):
        if config.are_word_suggestions_enabled():
            key = self.key
            keyboard = self.keyboard
            langdb = keyboard._languagedb

            lang_id = keyboard.get_lang_id()
            label = langdb.get_language_code(lang_id).capitalize()

            if label != key.get_label() or \
               not key.tooltip:
                key.set_labels({0: label})
                key.tooltip = langdb.get_language_full_name(lang_id)
                keyboard.invalidate_ui()

#---------------------------------------------------------
# deprecated buttons
#---------------------------------------------------------

class BCInputline(ButtonController):

    id = "inputline"

    def release(self, view, button, event_type):
        # hide the input line display when it is clicked
        self.keyboard.hide_input_line()


class BCAutoLearn(ButtonController):

    id = "learnmode"

    def release(self, view, button, event_type):
        config.wp.auto_learn = not config.wp.auto_learn

        # don't learn when turning auto_learn off
        if not config.wp.auto_learn:
            self.keyboard.discard_changes()

        # turning on auto_learn disables stealth_mode
        if config.wp.auto_learn and config.wp.stealth_mode:
            config.wp.stealth_mode = False

    def update(self):
        self.set_active(config.wp.auto_learn)


class BCAutoPunctuation(ButtonController):

    id = "punctuation"

    def release(self, view, button, event_type):
        config.wp.auto_punctuation = not config.wp.auto_punctuation
        self.keyboard.punctuator.reset()

    def update(self):
        self.set_active(config.wp.auto_punctuation)


class BCStealthMode(ButtonController):

    id = "stealthmode"

    def release(self, view, button, event_type):
        config.wp.stealth_mode = not config.wp.stealth_mode

        # don't learn, forget words when stealth mode is enabled
        if config.wp.stealth_mode:
            self.keyboard.discard_changes()

    def update(self):
        self.set_active(config.wp.stealth_mode)



