"""Exercise real confirmation rendering and responses against a fake backend."""
from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from optimus.ui import SecurityReviewDialog
from optimus.widgets.header_controls import install_global_window_reveal
from optimus.widgets.questions import make_yes_no_dialog
from optimus.widgets.theme import build_qss


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('answer', ['yes', 'keyboard_yes', 'no', 'escape', 'enter', 'close'])
def test_real_security_confirmation_only_explicit_yes_applies(qapp, answer):
    calls, errors = [], []
    backend = SimpleNamespace(apply_security_recommendation=lambda identifier, cancel_event=None:
                              calls.append(identifier) or {'status': 'completed'})
    owner = QWidget()
    owner.setStyleSheet(build_qss('cyber'))
    reveal = install_global_window_reveal(owner, config=SimpleNamespace(ui_motion_enabled=True))
    dialog = SecurityReviewDialog(backend, {'recommendations': [
        {'id': 'fixture-only', 'title': 'Disposable fixture recommendation',
         'reason': 'No system settings are changed.'}]}, owner)
    dialog.show()
    dialog.selection_control(0).setChecked(True)

    def respond():
        question = qapp.activeModalWidget()
        try:
            assert isinstance(question, QMessageBox)
            assert question.mask().isEmpty(), 'Consent prompt must not acquire a reveal mask.'
            assert question.defaultButton() is question.button(QMessageBox.No)
            assert question.escapeButton() is question.button(QMessageBox.No)
            assert question.button(QMessageBox.Yes).text() == 'Yes — apply'
            assert question.button(QMessageBox.No).text() == 'No — skip'
            if answer in ('yes', 'no'):
                QTest.mouseClick(question.button(QMessageBox.Yes if answer == 'yes' else QMessageBox.No), Qt.LeftButton)
            elif answer == 'keyboard_yes':
                yes = question.button(QMessageBox.Yes)
                yes.setFocus(Qt.TabFocusReason)
                QTest.keyClick(yes, Qt.Key_Return)
            elif answer == 'escape':
                QTest.keyClick(question, Qt.Key_Escape)
            elif answer == 'enter':
                QTest.keyClick(question, Qt.Key_Return)
            else:
                question.close()
        except Exception as error:
            errors.append(error)
            if isinstance(question, QMessageBox):
                question.done(QMessageBox.No)

    QTimer.singleShot(0, respond)
    try:
        dialog.apply_selected()
        deadline = time.monotonic() + 2
        while dialog._worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert not errors, errors
        assert calls == (['fixture-only'] if answer in ('yes', 'keyboard_yes') else [])
        assert dialog._worker is None
    finally:
        dialog.shutdown()
        dialog.pool.waitForDone(2000)
        reveal.shutdown()
        dialog.close()
        dialog.deleteLater()
        owner.close()
        owner.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize('theme', ['cyber', 'crt', 'slate'])
def test_labels_have_rendered_glyphs_before_and_during_hover(qapp, theme):
    owner = QWidget()
    owner.setStyleSheet(build_qss(theme))
    question = make_yes_no_dialog(owner, 'Fixture question', 'Apply a disposable fixture action?')
    try:
        question.show()
        qapp.processEvents()
        for standard in (QMessageBox.Yes, QMessageBox.No):
            button = question.button(standard)
            for hover in (False, True):
                if hover:
                    QTest.mouseMove(button, button.rect().center())
                    qapp.processEvents()
                image = button.grab().toImage()
                bright = sum(1 for y in range(image.height() // 2 - 7, image.height() // 2 + 7)
                             for x in range(12, image.width() - 12)
                             if min(image.pixelColor(x, y).getRgb()[:3]) > 190)
                assert bright > 10, f'{theme}/{standard}/hover={hover}: blank button label'
                assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 24
                assert button.height() >= 42
    finally:
        question.done(QMessageBox.No)
        question.deleteLater()
        owner.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize('theme', ['cyber', 'crt', 'slate'])
def test_security_selection_is_one_labeled_control_and_never_implies_consent(qapp, theme):
    from PySide6.QtWidgets import QCheckBox
    calls = []
    owner = QWidget()
    owner.setStyleSheet(build_qss(theme))
    dialog = SecurityReviewDialog(SimpleNamespace(apply_security_recommendation=lambda *a, **k: calls.append(a)),
        {'recommendations': [{'id': 'fixture-only', 'title': 'Review the fixture update source',
                              'reason': 'Disposable finding; no host operation is allowed.'}]}, owner)
    try:
        dialog.show()
        qapp.processEvents()
        item = dialog.table.item(0, 0)
        cell = dialog.table.cellWidget(0, 0)
        choice = dialog.selection_control(0)
        assert cell.findChildren(QCheckBox) == [choice]
        assert item.data(Qt.CheckStateRole) is None
        assert not item.flags() & Qt.ItemIsUserCheckable
        assert choice.text() == 'Review' and not choice.isChecked()
        assert 'Review the fixture update source' in choice.accessibleName()
        assert 'separate Yes or No' in choice.toolTip()
        assert not dialog.apply_button.isEnabled()
        QTest.mouseMove(choice, choice.rect().center())
        QTest.mouseClick(choice, Qt.LeftButton)
        qapp.processEvents()
        assert choice.isChecked() and dialog.apply_button.isEnabled()
        assert calls == [] and dialog.table.item(0, 3).text() == 'Not approved'
        choice.setFocus(Qt.TabFocusReason)
        QTest.keyClick(choice, Qt.Key_Space)
        assert not choice.isChecked() and not dialog.apply_button.isEnabled()
        assert calls == []
        assert choice.width() >= choice.fontMetrics().horizontalAdvance('Review') + 22
        assert cell.geometry().height() >= choice.sizeHint().height()
        # Reproduce a late theme/DPI font change, after the cell was created.
        choice.setStyleSheet('font-size:24px;')
        def rendered_fit():
            return (choice.font().pixelSize() == 24
                and choice.width() >= choice.fontMetrics().horizontalAdvance('Review') + 22
                and cell.geometry().height() >= choice.sizeHint().height() + 4)
        deadline = time.monotonic() + 1
        while not rendered_fit() and time.monotonic() < deadline:
            qapp.processEvents()
            QTest.qWait(5)
        assert rendered_fit(), 'The themed row did not finish laying out.'
        assert choice.width() >= choice.fontMetrics().horizontalAdvance('Review') + 22
        assert dialog.table.columnWidth(0) >= choice.minimumWidth() + 16
        assert cell.geometry().height() >= choice.sizeHint().height() + 4
        assert calls == [] and dialog.table.item(0, 3).text() == 'Not approved'
    finally:
        dialog.shutdown()
        dialog.close()
        dialog.deleteLater()
        owner.close()
        owner.deleteLater()
        qapp.processEvents()
