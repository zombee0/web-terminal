(() => {
  'use strict';

  const app = document.querySelector('#app');
  const host = document.querySelector('#term');
  const menu = document.querySelector('#selection-menu');
  const startHandle = document.querySelector('#selection-start');
  const endHandle = document.querySelector('#selection-end');
  const dialog = document.querySelector('#clipboard-dialog');
  const field = document.querySelector('#clipboard-text');
  const submit = document.querySelector('#clipboard-submit');
  const notice = document.querySelector('#notice');
  const coarsePointer = matchMedia('(any-pointer: coarse)');
  host.classList.toggle('touch-input', coarsePointer.matches || navigator.maxTouchPoints > 0);
  const term = new Terminal({
    cursorBlink: true, convertEol: true, scrollback: 10000,
    allowProposedApi: true, // Required by the pinned Unicode11 addon's public API.
    fontFamily: "ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Noto Sans Mono CJK SC','PingFang SC','Microsoft YaHei',monospace",
    theme: { background: '#090d12', selectionBackground: '#3b6189', selectionInactiveBackground: '#3b6189' }
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  if (window.Unicode11Addon) {
    try {
      term.loadAddon(new Unicode11Addon.Unicode11Addon());
      term.unicode.activeVersion = '11';
    } catch (error) {
      console.warn('Unicode addon disabled', error);
    }
  }
  term.open(host);
  const screen = host.querySelector('.xterm-screen');
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') +
    location.host + location.pathname.replace(/\/?$/, '/') + 'ws' + location.search);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    resize();
    sendSize();
    if (!coarsePointer.matches) term.focus();
  };
  ws.onmessage = event => term.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
  ws.onclose = () => term.write('\r\n[Connection closed]\r\n');
  ws.onerror = () => term.write('\r\n[Connection error]\r\n');
  term.onData(data => {
    dismiss();
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  });
  // Legacy mouse protocols emit raw bytes, not UTF-8 text.
  term.onBinary(data => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(Uint8Array.from(data, char => char.charCodeAt(0)));
    }
  });

  let selecting = false;
  let menuPoint = { x: 0, y: 0 };
  let gesture = null;
  let holdTimer = 0;
  let edgeTimer = 0;
  let noticeTimer = 0;
  let resizeFrame = 0;
  let lastTouch = 0;
  let clipboardBusy = false;
  const clamp = (value, min, max) => Math.max(min, Math.min(value, max));

  function sendSize() {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    }
  }

  function resize() {
    const viewport = window.visualViewport;
    // Follow the software keyboard without reflowing the buffer during pinch zoom.
    if (viewport && Math.abs(viewport.scale - 1) < .01) {
      app.style.height = viewport.height + 'px';
      app.style.left = viewport.offsetLeft + 'px';
      app.style.top = viewport.offsetTop + 'px';
      document.documentElement.style.setProperty('--viewport-height', viewport.height + 'px');
      document.documentElement.style.setProperty('--viewport-center', (viewport.offsetTop + viewport.height / 2) + 'px');
    }
    const size = fit.proposeDimensions();
    if (size && (size.cols !== term.cols || size.rows !== term.rows)) {
      cancelGesture();
      dismiss(); // A column reflow invalidates cell coordinates.
      fit.fit();
      sendSize();
    }
    renderSelection();
  }

  function scheduleResize() {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(resize);
  }
  window.addEventListener('resize', scheduleResize);
  window.visualViewport?.addEventListener('resize', scheduleResize);
  window.visualViewport?.addEventListener('scroll', scheduleResize);
  document.fonts?.ready.then(scheduleResize);

  function tell(message) {
    clearTimeout(noticeTimer);
    notice.textContent = message;
    notice.hidden = false;
    noticeTimer = setTimeout(() => { notice.hidden = true; }, 2500);
  }

  function dismiss() {
    selecting = false;
    menu.hidden = startHandle.hidden = endHandle.hidden = true;
    term.clearSelection();
  }

  function geometry() {
    const rect = screen.getBoundingClientRect();
    return { rect, width: rect.width / term.cols, height: rect.height / term.rows };
  }

  function pointToCell(x, y, boundary = false) {
    const { rect, width, height } = geometry();
    const column = (x - rect.left) / width;
    const col = clamp(boundary ? Math.round(column) : Math.floor(column), 0, boundary ? term.cols : term.cols - 1);
    const row = clamp(Math.floor((y - rect.top) / height), 0, term.rows - 1) + term.buffer.active.viewportY;
    return { col, row: Math.min(row, term.buffer.active.length - 1) };
  }

  function indexAt(point) { return point.row * term.cols + point.col; }
  function cellAt(index) {
    return term.buffer.active.getLine(Math.floor(index / term.cols))?.getCell(index % term.cols);
  }
  function cellStart(index) {
    return cellAt(index)?.getWidth() === 0 && index > 0 ? index - 1 : index;
  }
  function cellEnd(index) { return index + Math.max(1, cellAt(index)?.getWidth() || 1); }
  function visibleEnd(selection) {
    return selection.end.x === 0 && selection.end.y > selection.start.y
      ? { x: term.cols, y: selection.end.y - 1 } : selection.end;
  }

  function selectWord(x, y) {
    const point = pointToCell(x, y);
    let start = cellStart(indexAt(point));
    let end = cellEnd(start);
    const isWord = index => /\S/u.test(cellAt(index)?.getChars() || ' ');
    // Follow wrapped lines, but never merge separate output lines. Cell widths
    // keep CJK, emoji and combining characters aligned with the rendered text.
    if (isWord(start)) {
      while (start > 0) {
        if (start % term.cols === 0 && !term.buffer.active.getLine(start / term.cols)?.isWrapped) break;
        const previous = cellStart(start - 1);
        if (!isWord(previous)) break;
        start = previous;
      }
      while (end < term.buffer.active.length * term.cols) {
        if (end % term.cols === 0 && !term.buffer.active.getLine(end / term.cols)?.isWrapped) break;
        if (!isWord(end)) break;
        end = cellEnd(end);
      }
    }
    selecting = true;
    menuPoint = { x, y };
    term.select(start % term.cols, Math.floor(start / term.cols), end - start);
    renderSelection();
  }

  function renderSelection() {
    if (!selecting) return;
    const bounds = app.getBoundingClientRect();
    const { rect, width, height } = geometry();
    const selection = term.getSelectionPosition();
    function placeHandle(handle, point, end) {
      const row = point.y - term.buffer.active.viewportY;
      handle.hidden = row < 0 || row >= term.rows;
      handle.style.left = clamp(rect.left - bounds.left + point.x * width, 22, bounds.width - 22) + 'px';
      handle.style.top = clamp(rect.top - bounds.top + (row + (end ? 1 : 0)) * height, 28, bounds.height - 28) + 'px';
    }
    if (selection && term.getSelection().trim()) {
      placeHandle(startHandle, selection.start, false);
      placeHandle(endHandle, visibleEnd(selection), true);
    } else {
      startHandle.hidden = endHandle.hidden = true;
    }
    menu.querySelector('[data-action="copy"]').disabled = !term.getSelection().trim();
    menu.hidden = gesture?.mode === 'handle' || gesture?.mode === 'extend';
    if (!menu.hidden) {
      const x = menuPoint.x - bounds.left;
      const y = menuPoint.y - bounds.top;
      const top = y - menu.offsetHeight - 60;
      menu.style.left = clamp(x - menu.offsetWidth / 2, 4, bounds.width - menu.offsetWidth - 4) + 'px';
      menu.style.top = clamp(top >= 4 ? top : y + height + 60, 4, bounds.height - menu.offsetHeight - 4) + 'px';
    }
  }
  term.onSelectionChange(renderSelection);
  term.onScroll(renderSelection);
  let bufferType = term.buffer.active.type;
  term.onWriteParsed(() => {
    if (bufferType !== term.buffer.active.type) {
      bufferType = term.buffer.active.type;
      cancelGesture();
      dismiss();
    }
    renderSelection();
  });

  function stopEvent(event) {
    if (event.cancelable) event.preventDefault();
    event.stopPropagation();
  }
  function cancelGesture() {
    clearTimeout(holdTimer);
    clearTimeout(edgeTimer);
    gesture = null;
  }

  app.addEventListener('touchstart', event => {
    if (event.touches.length !== 1) {
      cancelGesture();
      renderSelection();
      return; // Allow browser pinch zoom.
    }
    const handle = event.target.closest('.selection-handle');
    if (!handle && !host.contains(event.target)) return;
    stopEvent(event); // Suppress xterm's touch scrolling and synthetic mouse clicks.
    cancelGesture();
    lastTouch = Date.now();
    const touch = event.touches[0];
    gesture = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
      originX: touch.clientX, originY: touch.clientY, lastY: touch.clientY,
      remainder: 0, mode: handle ? 'handle' : 'pending', hadSelection: selecting };
    if (handle) {
      const selection = term.getSelectionPosition();
      if (!selection) { cancelGesture(); return; }
      gesture.edge = handle === startHandle ? 'start' : 'end';
      const anchor = gesture.edge === 'start' ? selection.end : selection.start;
      gesture.anchor = anchor.y * term.cols + anchor.x;
      // Preserve the offset between the fingertip and the selection boundary.
      const boundary = gesture.edge === 'start' ? selection.start : visibleEnd(selection);
      const { rect, width, height } = geometry();
      gesture.offsetX = rect.left + boundary.x * width - touch.clientX;
      gesture.offsetY = rect.top + (boundary.y - term.buffer.active.viewportY + .5) * height - touch.clientY;
      renderSelection();
      return;
    }
    holdTimer = setTimeout(() => {
      if (!gesture || gesture.mode !== 'pending') return;
      gesture.mode = 'held';
      selectWord(gesture.x, gesture.y);
      const selection = term.getSelectionPosition();
      gesture.anchor = selection.start.y * term.cols + selection.start.x;
      gesture.initialEnd = selection.end.y * term.cols + selection.end.x;
    }, 450);
  }, { passive: false, capture: true });

  function extendSelection() {
    if (!gesture) return;
    const g = gesture;
    const point = pointToCell(g.x + (g.offsetX || 0), g.y + (g.offsetY || 0), g.mode === 'handle');
    let index = indexAt(point);
    let start, end;
    if (g.mode === 'handle') {
      if (g.edge === 'start') {
        start = cellStart(Math.min(index, g.anchor - 1));
        end = g.anchor;
      } else {
        start = g.anchor;
        if (cellAt(index)?.getWidth() === 0) index++;
        end = Math.max(index, cellEnd(start));
      }
    } else {
      index = cellStart(index);
      start = Math.min(g.anchor, index);
      end = index < g.anchor ? g.initialEnd : cellEnd(index);
    }
    term.select(start % term.cols, Math.floor(start / term.cols), end - start);
    menuPoint = { x: g.x, y: g.y };
    renderSelection();
    clearTimeout(edgeTimer);
    const { rect, height } = geometry();
    const direction = g.y < rect.top + height ? -1 : g.y > rect.bottom - height ? 1 : 0;
    if (direction && term.buffer.active.type === 'normal') {
      edgeTimer = setTimeout(() => {
        term.scrollLines(direction);
        extendSelection();
      }, 80);
    }
  }

  function scrollTouch(delta, x, y) {
    const { rect, height } = geometry();
    gesture.remainder += delta / height;
    const lines = Math.trunc(gesture.remainder);
    gesture.remainder -= lines;
    if (!lines) return;
    if (term.buffer.active.type === 'normal' && term.modes.mouseTrackingMode === 'none') {
      term.scrollLines(lines);
    } else {
      // Let xterm encode the active mouse protocol or alternate-screen keys.
      // One event per line also works with applications such as tmux.
      for (let i = 0; i < Math.abs(lines); i++) {
        term.element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true,
          deltaY: Math.sign(lines), deltaMode: 1,
          clientX: clamp(x, rect.left + 1, rect.right - 1), clientY: clamp(y, rect.top + 1, rect.bottom - 1) }));
      }
    }
  }

  document.addEventListener('touchmove', event => {
    if (!gesture || event.touches.length !== 1) return;
    const touch = [...event.touches].find(item => item.identifier === gesture.id);
    if (!touch) return;
    stopEvent(event);
    const g = gesture;
    g.x = touch.clientX;
    g.y = touch.clientY;
    const moved = Math.hypot(g.x - g.originX, g.y - g.originY) > 10;
    if (g.mode === 'pending' && moved) {
      clearTimeout(holdTimer);
      g.mode = 'scroll';
      dismiss();
    } else if (g.mode === 'held' && moved) {
      g.mode = 'extend';
    }
    if (g.mode === 'scroll') scrollTouch(g.lastY - g.y, g.x, g.y);
    if (g.mode === 'handle' || g.mode === 'extend') extendSelection();
    g.lastY = g.y;
  }, { passive: false, capture: true });

  document.addEventListener('touchend', event => {
    if (!gesture) return;
    stopEvent(event);
    const g = gesture;
    cancelGesture();
    if (g.mode === 'pending') {
      dismiss();
      if (!g.hadSelection) term.focus();
    }
    renderSelection();
  }, { passive: false, capture: true });
  document.addEventListener('touchcancel', () => { cancelGesture(); renderSelection(); }, { capture: true });
  window.addEventListener('blur', cancelGesture);
  document.addEventListener('visibilitychange', cancelGesture);
  host.addEventListener('contextmenu', event => {
    if (Date.now() - lastTouch < 1500) stopEvent(event);
  }, { capture: true });
  host.addEventListener('mousedown', () => { if (selecting) dismiss(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && selecting && !dialog.open) {
      stopEvent(event);
      dismiss();
    }
  }, { capture: true });

  const outputDialog = document.querySelector('#output-dialog');
  const outputScroll = document.querySelector('#output-scroll');
  const outputHistory = document.querySelector('#output-history');
  const outputScreen = document.querySelector('#output-screen');

  function bufferText(buffer) {
    const parts = [];
    for (let row = 0; row < buffer.length; row++) {
      const line = buffer.getLine(row);
      const wrapped = buffer.getLine(row + 1)?.isWrapped;
      let end = line.length;
      if (wrapped) {
        // A wide character can leave an unused cell before wrapping. Remove
        // that padding, but retain actual spaces at a logical line boundary.
        while (end > 0) {
          const cell = line.getCell(end - 1);
          if (cell.getWidth() !== 1 || cell.getChars()) break;
          end--;
        }
      }
      parts.push(line.translateToString(!wrapped, 0, end));
      if (!wrapped) parts.push('\n');
    }
    return parts.join('').replace(/\n+$/, '');
  }

  function refreshOutput() {
    const position = outputScroll.scrollTop;
    const atLatest = outputScroll.scrollHeight - position - outputScroll.clientHeight < 2;
    const fullscreen = term.buffer.active.type === 'alternate';
    outputHistory.textContent = bufferText(term.buffer.normal);
    outputScreen.textContent = fullscreen ? bufferText(term.buffer.alternate) : '';
    document.querySelector('#output-current').hidden = !fullscreen;
    document.querySelector('#output-help').textContent = fullscreen
      ? 'Retained history and current screen. Earlier fullscreen output that was overwritten is unavailable. Refresh to update.'
      : 'Retained output from this tab. Scroll to read; Refresh to include new output.';
    outputScroll.scrollTop = atLatest ? outputScroll.scrollHeight : position;
  }

  document.querySelector('#browse-output').addEventListener('click', () => {
    cancelGesture();
    dismiss();
    term.blur();
    outputDialog.showModal();
    refreshOutput();
    outputScroll.scrollTop = outputScroll.scrollHeight;
  });
  document.querySelector('#output-refresh').addEventListener('click', refreshOutput);
  document.querySelector('#output-latest').addEventListener('click', () => {
    outputScroll.scrollTop = outputScroll.scrollHeight;
  });
  document.querySelector('#output-close').addEventListener('click', () => outputDialog.close());
  outputDialog.addEventListener('close', () => {
    outputHistory.textContent = outputScreen.textContent = '';
  });

  function openClipboard(mode, text = '') {
    dismiss();
    const copying = mode === 'copy';
    dialog.dataset.mode = mode;
    document.querySelector('#clipboard-title').textContent = copying ? 'Copy selected text' : 'Paste into terminal';
    document.querySelector('#clipboard-help').textContent = copying
      ? 'Long press the selected text and choose Copy.'
      : 'Long press in the field and choose Paste, then tap Paste into terminal.';
    document.querySelector('#clipboard-cancel').textContent = copying ? 'Done' : 'Cancel';
    submit.hidden = copying;
    field.readOnly = copying;
    field.value = text;
    dialog.showModal();
    field.focus();
    if (copying) field.select();
  }

  function connected() {
    if (ws.readyState === WebSocket.OPEN) return true;
    tell('Terminal is disconnected. Reconnect before pasting.');
    return false;
  }

  function paste(text) {
    if (!connected()) return false;
    if (!text) { tell('Nothing to paste.'); return false; }
    dismiss();
    term.paste(text); // Preserve xterm's newline handling and bracketed paste mode.
    term.focus();
    return true;
  }

  async function copySelection() {
    const text = term.getSelection();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      dismiss();
      tell('Copied');
    } catch {
      openClipboard('copy', text);
    }
  }

  async function pasteClipboard() {
    if (!connected()) return;
    try {
      const text = await navigator.clipboard.readText();
      paste(text);
    } catch {
      openClipboard('paste');
    }
  }

  menu.addEventListener('click', async event => {
    const action = event.target.closest('button')?.dataset.action;
    if (clipboardBusy) return;
    if (action === 'dismiss') dismiss();
    if (action === 'select-all') { term.selectAll(); renderSelection(); }
    if (action === 'copy' || action === 'paste') {
      clipboardBusy = true;
      try {
        if (action === 'copy') await copySelection();
        else await pasteClipboard();
      } finally { clipboardBusy = false; }
    }
  });
  document.querySelector('#clipboard-cancel').addEventListener('click', () => dialog.close());
  document.querySelector('#clipboard-form').addEventListener('submit', event => {
    event.preventDefault();
    if (dialog.dataset.mode === 'paste' && connected() && field.value) {
      const text = field.value;
      dialog.close();
      paste(text);
    }
  });
  // Clear clipboard contents from the fallback UI as soon as it closes.
  dialog.addEventListener('close', () => { field.value = ''; });
  resize();
})();
