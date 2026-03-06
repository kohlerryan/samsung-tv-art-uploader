/**
 * Frame TV Art Card v0.2.0-beta.2
 */

class FrameTVArtCard extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._dropdownOpen = false;
    this._lastStateHash = '';
    this._statusMessage = '';
    this._refreshAck = { status: '', message: '', req_id: '', updated: 0 };
    this._refreshRequest = { req_id: '', updated: 0 };
    this._syncAck = { status: '', message: '', req_id: '', updated: 0 };
    this._refreshAckUnsubscribe = null;
    this._refreshCmdUnsubscribe = null;
    this._syncAckUnsubscribe = null;
    this._refreshSubscribing = false;
    this._setStatus = null;
    this._refreshInfoMsg = null;
    this._refreshInProgress = false;
    this._refreshProgressMsg = '';
    this._refreshProgressLog = [];
    this._docClickHandler = null;
    this._pollAppliedTimer = null;
    this._staleClearTimer = null;

    this._slideshowSeq = false;
    this._slideshowUpdateMins = 0;
    this._slideshowMode = 'auto';
    this._slideshowAttrsUnsubscribe = null;
    this._slideshowAvailable = [];
    this._slideshowCurrentPaths = [];
    this._slideshowOverridePaths = [];
    this._slideshowSelected = new Set();
    this._slideshowMaxUploads = 10;
    this._slideshowUploading = false;
    this._overridePanelOpen = false;
    this._slideshowAvailUnsubscribe = null;
    this._slideshowPostClear = false;           // blocks stale current_paths reseed after clear
    this._slideshowClearRefreshPending = false;  // set when Clear fires collections/refresh
    // Restore progress log if page was refreshed mid-sync (max 15 min TTL)
    try {
      const _raw = sessionStorage.getItem('ftvHaRefreshLog');
      if (_raw && sessionStorage.getItem('ftvHaRefreshActive')) {
        const _entry = JSON.parse(_raw);
        const _age = Date.now() - (_entry.ts || 0);
        if (_age < 15 * 60 * 1000) {
          this._refreshProgressLog = _entry.log || [];
          this._refreshInProgress = true;
          // Safety valve: if no new ack messages arrive within 30s, assume the
          // container already finished and clear the stale state.
          this._staleClearTimer = setTimeout(() => {
            if (this._refreshInProgress) {
              this._refreshInProgress = false;
              this._refreshProgressLog = [];
              try { sessionStorage.removeItem('ftvHaRefreshLog'); sessionStorage.removeItem('ftvHaRefreshActive'); } catch(_) {}
              this._lastStateHash = '';
              if (this._hass) this._render();
            }
          }, 30000);
        } else {
          sessionStorage.removeItem('ftvHaRefreshLog'); sessionStorage.removeItem('ftvHaRefreshActive');
        }
      }
    } catch(_) {}
  }

  setConfig(config) {
    this._config = {
      title: config.title || 'Frame TV Art',
      icon: config.icon || 'mdi:palette',
      // MQTT-first configuration: entity that exposes settings via attributes
      settings_entity: config.settings_entity || 'sensor.frame_tv_art_settings',
      // Default to MQTT-discovered sensors provided by the container
      collections_entity: config.collections_entity || 'sensor.frame_tv_art_collections',
      selected_artwork_file_entity: config.selected_artwork_file_entity || 'sensor.frame_tv_art_selected_artwork',
      selected_collections_entity: config.selected_collections_entity || 'sensor.frame_tv_art_selected_collections',
      refresh_cmd_topic: config.refresh_cmd_topic || 'frame_tv/cmd/collections/refresh',
      refresh_ack_topic: config.refresh_ack_topic || 'frame_tv/ack/collections/refresh',
      sync_ack_topic: config.sync_ack_topic || 'frame_tv/ack/settings/sync_collections',
      slideshow_attr_topic: config.slideshow_attr_topic || 'frame_tv/slideshow/attributes',
      slideshow_available_topic: config.slideshow_available_topic || 'frame_tv/slideshow/available',
      // Optional: allow overriding legacy helpers if desired
      add_button_entity: config.add_button_entity, // legacy keys no longer used
      remove_button_entity: config.remove_button_entity, // legacy keys no longer used
      clear_button_entity: config.clear_button_entity, // legacy keys no longer used
      // Single base path for images served by Home Assistant (/local maps to /config/www)
      image_path: config.image_path || '/local/images/frame_tv_art_collections',
      standby_image_path: config.standby_image_path,
      ...config
    };
    // don't eagerly build standby path here; compute based on protocol when needed
    this._lastStateHash = '';
    // No CSV fetch required; all metadata comes from MQTT sensor attributes
  }

  _getBaseImagePath() {
    // Single unified base path (no http/https split needed when using /local)
    return this._config.image_path || '';
  }

  _preloadThumbnails(images) {
    if (!images || !images.length) return;
    const basePath = this._getBaseImagePath();
    const urls = images.map(img =>
      `${basePath}/${encodeURIComponent(img.folder)}/${encodeURIComponent(img.file)}`
    );
    // Load up to 6 concurrently to warm the browser cache without flooding
    const CONCURRENCY = 6;
    let idx = 0;
    const next = () => {
      if (idx >= urls.length) return;
      const url = urls[idx++];
      const img = new Image();
      img.onload = img.onerror = next;
      img.src = url;
    };
    for (let i = 0; i < Math.min(CONCURRENCY, urls.length); i++) next();
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureRefreshSubscriptions();
    
    // Sync baseline from HA state; keep staged when dropdown is open
    const fromState = this._getSelectedCollections();
    this._baselineSelected = Array.isArray(fromState) ? [...fromState] : [];
    if (!Array.isArray(this._currentSelected) || !this._dropdownOpen) {
      this._currentSelected = [...this._baselineSelected];
    }
    
    // Don't re-render if dropdown is open
    if (this._dropdownOpen) return;
    
    // Only re-render if relevant state changed
    const newHash = this._getStateHash();
    if (newHash === this._lastStateHash) return;
    this._lastStateHash = newHash;
    
    this._render();
  }

  _getStateHash() {
    // Create a hash of the states we care about
    const file = this._getSelectedData().file || '';
    const selected = this._getState(this._config.selected_collections_entity);
    const options = this._getOptions(this._config.collections_entity).join(',');
    const ackStatus = (this._refreshAck && this._refreshAck.status) || '';
    const ackMessage = (this._refreshAck && this._refreshAck.message) || '';
    const ackReqId = (this._refreshAck && this._refreshAck.req_id) || '';
    const syncStatus = (this._syncAck && this._syncAck.status) || '';
    return `${file}|${selected}|${options}|${ackStatus}|${ackMessage}|${ackReqId}|${syncStatus}|${this._refreshInProgress}|${this._slideshowMode}|${this._overridePanelOpen}|${this._slideshowSeq}|${this._slideshowUpdateMins}|${this._slideshowMaxUploads}|${this._slideshowUploading}`;
  }

  disconnectedCallback() {
    if (typeof this._refreshAckUnsubscribe === 'function') {
      try { this._refreshAckUnsubscribe(); } catch (_) {}
    }
    if (typeof this._refreshCmdUnsubscribe === 'function') {
      try { this._refreshCmdUnsubscribe(); } catch (_) {}
    }
    if (typeof this._syncAckUnsubscribe === 'function') {
      try { this._syncAckUnsubscribe(); } catch (_) {}
    }
    if (typeof this._slideshowAttrsUnsubscribe === 'function') {
      try { this._slideshowAttrsUnsubscribe(); } catch (_) {}
    }
    if (typeof this._slideshowAvailUnsubscribe === 'function') {
      try { this._slideshowAvailUnsubscribe(); } catch (_) {}
    }
    this._refreshAckUnsubscribe = null;
    this._refreshCmdUnsubscribe = null;
    this._syncAckUnsubscribe = null;
    this._slideshowAttrsUnsubscribe = null;
    this._slideshowAvailUnsubscribe = null;
    this._refreshSubscribing = false;
    // Clean up document-level listener and polling timer
    if (this._docClickHandler) {
      document.removeEventListener('click', this._docClickHandler, { capture: true });
      this._docClickHandler = null;
    }
    if (this._pollAppliedTimer) {
      clearTimeout(this._pollAppliedTimer);
      this._pollAppliedTimer = null;
    }
  }

  _ensureRefreshSubscriptions() {
    if (!this._hass || !this._hass.connection) return;
    if (this._refreshSubscribing) return;
    if (this._refreshAckUnsubscribe && this._refreshCmdUnsubscribe && this._syncAckUnsubscribe && this._slideshowAttrsUnsubscribe && this._slideshowAvailUnsubscribe) return;
    this._refreshSubscribing = true;

    const ensureAck = this._refreshAckUnsubscribe
      ? Promise.resolve(this._refreshAckUnsubscribe)
      : this._hass.connection.subscribeMessage(
          (msg) => this._handleRefreshAckMessage(msg),
          { type: 'mqtt/subscribe', topic: this._config.refresh_ack_topic }
        );

    const ensureCmd = this._refreshCmdUnsubscribe
      ? Promise.resolve(this._refreshCmdUnsubscribe)
      : this._hass.connection.subscribeMessage(
          (msg) => this._handleRefreshRequestMessage(msg),
          { type: 'mqtt/subscribe', topic: this._config.refresh_cmd_topic }
        );

    const ensureSyncAck = this._syncAckUnsubscribe
      ? Promise.resolve(this._syncAckUnsubscribe)
      : this._hass.connection.subscribeMessage(
          (msg) => this._handleSyncAckMessage(msg),
          { type: 'mqtt/subscribe', topic: this._config.sync_ack_topic }
        );

    const ensureSlideshowAttrs = this._slideshowAttrsUnsubscribe
      ? Promise.resolve(this._slideshowAttrsUnsubscribe)
      : this._hass.connection.subscribeMessage(
          (msg) => this._handleSlideshowAttrsMessage(msg),
          { type: 'mqtt/subscribe', topic: this._config.slideshow_attr_topic }
        );

    const ensureSlideshowAvail = this._slideshowAvailUnsubscribe
      ? Promise.resolve(this._slideshowAvailUnsubscribe)
      : this._hass.connection.subscribeMessage(
          (msg) => this._handleSlideshowAvailableMessage(msg),
          { type: 'mqtt/subscribe', topic: this._config.slideshow_available_topic }
        );

    Promise.all([ensureAck, ensureCmd, ensureSyncAck, ensureSlideshowAttrs, ensureSlideshowAvail])
      .then(([ackUnsub, cmdUnsub, syncAckUnsub, slideshowAttrsUnsub, slideshowAvailUnsub]) => {
        if (!this._refreshAckUnsubscribe) this._refreshAckUnsubscribe = ackUnsub;
        if (!this._refreshCmdUnsubscribe) this._refreshCmdUnsubscribe = cmdUnsub;
        if (!this._syncAckUnsubscribe) this._syncAckUnsubscribe = syncAckUnsub;
        if (!this._slideshowAttrsUnsubscribe) this._slideshowAttrsUnsubscribe = slideshowAttrsUnsub;
        if (!this._slideshowAvailUnsubscribe) this._slideshowAvailUnsubscribe = slideshowAvailUnsub;
      })
      .catch(() => {})
      .finally(() => {
        this._refreshSubscribing = false;
      });
  }

  _parseJsonPayload(message) {
    const raw = message && Object.prototype.hasOwnProperty.call(message, 'payload')
      ? message.payload
      : message;
    if (raw == null) return {};
    if (typeof raw === 'object') return raw;
    if (typeof raw !== 'string') return {};
    try {
      return JSON.parse(raw);
    } catch (_) {
      return {};
    }
  }

  _syncRefreshAckStatus() {
    const ackStatus = String((this._refreshAck && this._refreshAck.status) || '').toLowerCase();
    const ackMessage = String((this._refreshAck && this._refreshAck.message) || '').trim();
    const ackReqId = this._refreshAck && this._refreshAck.req_id != null ? String(this._refreshAck.req_id) : '';
    const reqMatches = !this._lastRefreshReqId || !ackReqId || String(this._lastRefreshReqId) === ackReqId;
    if (!reqMatches) return;

    const renderLog = () => {
      const infoEl = this.querySelector('.ftv-info');
      const logEl = this.querySelector('.ftv-refresh-log');
      if (infoEl) infoEl.innerHTML = this._refreshProgressLog.length ? `<div>${this._refreshProgressLog[0]}</div>` : '';
      if (logEl) logEl.innerHTML = this._refreshProgressLog.slice(1).map(l => `<div>${l}</div>`).join('');
      try { sessionStorage.setItem('ftvHaRefreshLog', JSON.stringify({ log: this._refreshProgressLog, ts: Date.now() })); sessionStorage.setItem('ftvHaRefreshActive', '1'); } catch(_) {}
    };

    const appendProgress = (msg) => {
      if (this._staleClearTimer) { clearTimeout(this._staleClearTimer); this._staleClearTimer = null; }
      if (!this._refreshInProgress) {
        // First message: lock the display, seed the log, trigger full re-render with standby bg
        this._refreshProgressLog = ['Please stand by as artwork is loaded...'];
        if (msg) this._refreshProgressLog.push(msg);
        this._refreshProgressMsg = msg;
        this._refreshInProgress = true;
        this._lastStateHash = '';
        this._render();
      } else {
        // Deduplicate: drop this message if it already appears in the log.
        // This silently absorbs duplicate deliveries caused by accumulated MQTT
        // subscriptions (HA WS reconnects build up extra subscriptions over time).
        if (this._refreshProgressLog.includes(msg)) return;
        this._refreshProgressLog.push(msg);
        this._refreshProgressMsg = msg;
        renderLog();
      }
    };

    const finishProgress = (msg, delayMs) => {
      // Guard: if not in progress, a late duplicate finish message arrived — drop it.
      if (!this._refreshInProgress) return;
      // Deduplicate finish messages (multiple subscriptions can deliver the same ok/error).
      if (this._refreshProgressLog.includes(msg)) return;
      this._refreshProgressLog.push(msg);
      this._refreshProgressMsg = msg;
      renderLog();
      setTimeout(() => {
        if (this._refreshInProgress) {
          this._refreshInProgress = false;
          this._refreshProgressMsg = '';
          this._refreshProgressLog = [];
          try { sessionStorage.removeItem('ftvHaRefreshLog'); sessionStorage.removeItem('ftvHaRefreshActive'); } catch(_) {}
          this._lastStateHash = '';
          this._render();
        }
      }, delayMs);
    };

    if (ackStatus === 'queued') {
      appendProgress(ackMessage || 'Refresh queued. Waiting for backend...');
    } else if (ackStatus === 'started') {
      appendProgress(ackMessage || 'Switching TV to standby...');
    } else if (ackStatus === 'progress') {
      appendProgress(ackMessage || 'Refresh in progress...');
    } else if (ackStatus === 'ok') {
      finishProgress(ackMessage || 'Refresh complete.', 8000);
    } else if (ackStatus === 'error') {
      finishProgress(ackMessage ? `Refresh failed: ${ackMessage}` : 'Refresh failed.', 12000);
    }
  }

  _handleRefreshRequestMessage(message) {
    const payload = this._parseJsonPayload(message);
    const reqId = payload && payload.req_id != null ? String(payload.req_id) : '';
    this._refreshRequest = { req_id: reqId, updated: Date.now() };
    const reqMatches = !this._lastRefreshReqId || !reqId || String(this._lastRefreshReqId) === reqId;
    if (reqMatches) {
      this._refreshAck = {
        status: 'queued',
        message: 'Refresh request queued. Waiting for backend confirmation...',
        req_id: reqId,
        updated: Date.now(),
      };
      this._syncRefreshAckStatus();
    }
  }

  _handleRefreshAckMessage(message) {
    const payload = this._parseJsonPayload(message);
    const status = String((payload && payload.status) || '').toLowerCase();
    const messageText = String((payload && payload.message) || '').trim();
    const reqId = payload && payload.req_id != null ? String(payload.req_id) : '';
    // Adopt any incoming req_id when a new reseed starts and we're not already
    // locked. This ensures auto-triggered reseeds (selection change, startup)
    // are never silently filtered by a stale _lastRefreshReqId from a prior button press.
    if ((status === 'started' || status === 'queued') && !this._refreshInProgress) {
      this._lastRefreshReqId = reqId || null;
    }
    this._refreshAck = {
      status,
      message: messageText,
      req_id: reqId,
      updated: Date.now(),
    };
    this._syncRefreshAckStatus();
  }

  _handleSlideshowAttrsMessage(message) {
    const payload = this._parseJsonPayload(message);
    const mode = String((payload && payload.mode) || 'auto').toLowerCase();
    const prevMode = this._slideshowMode;
    const prevUploading = this._slideshowUploading;
    this._slideshowCurrentPaths = (payload && Array.isArray(payload.current_paths)) ? payload.current_paths : [];
    this._slideshowOverridePaths = (payload && Array.isArray(payload.override_paths)) ? payload.override_paths : [];
    this._slideshowMaxUploads = parseInt((payload && payload.max_uploads) || 10, 10);
    this._slideshowUploading = !!(payload && payload.uploading);
    this._slideshowSeq = !!(payload && payload.sequential);
    this._slideshowUpdateMins = parseInt((payload && payload.update_minutes) || 0, 10);
    this._slideshowMode = mode;
    const uploadJustFinished = prevUploading && !this._slideshowUploading;
    if (mode !== prevMode) {
      if (mode === 'override') {
        // New override applied (auto → override): paths are authoritative, reset post-clear flag.
        this._slideshowPostClear = false;
        if (this._slideshowOverridePaths.length) {
          this._slideshowSelected = new Set(this._slideshowOverridePaths);
        }
        if (this._hass) {
          this._hass.callService('mqtt', 'publish', {
            topic: 'frame_tv/cmd/slideshow/available/request',
            payload: JSON.stringify({ req_id: Date.now() }),
            qos: 1, retain: false,
          }).catch(() => {});
        }
      } else {
        // Override cleared (override → auto): current_paths is stale (still has the union of
        // all uploaded files). Clear selection and block fallback reseeds until a cycle runs.
        this._slideshowPostClear = true;
        this._slideshowSelected = new Set();
        // Request a fresh available list so the grid repaints with nothing selected
        if (this._hass) {
          this._hass.callService('mqtt', 'publish', {
            topic: 'frame_tv/cmd/slideshow/available/request',
            payload: JSON.stringify({ req_id: Date.now() }),
            qos: 1, retain: false,
          }).catch(() => {});
        }
      }
      this._lastStateHash = '';
      if (this._hass) this._render();
    } else {
      // Mode unchanged. If override, resync selection from server-authoritative override_paths
      // (handles server-side pruning of deleted files from override state).
      if (mode === 'override' && !this._slideshowUploading && !this._slideshowPostClear
          && this._slideshowOverridePaths.length) {
        this._slideshowSelected = new Set(this._slideshowOverridePaths);
      }
      if (this._overridePanelOpen) {
        if (uploadJustFinished) {
          this._slideshowReverting = false;
          if (mode === 'override') {
            // Override upload completed: override_paths are confirmed by server.
            this._slideshowPostClear = false;
            this._slideshowClearRefreshPending = false;
            if (this._slideshowOverridePaths.length) {
              this._slideshowSelected = new Set(this._slideshowOverridePaths);
            }
          } else if (this._slideshowClearRefreshPending) {
            // Full collections/refresh triggered by Clear Override has completed.
            // current_paths is now fresh and authoritative — seed the grid from it.
            this._slideshowClearRefreshPending = false;
            this._slideshowPostClear = false;
            this._slideshowSelected = new Set(this._slideshowCurrentPaths);
          } else {
            // Background auto cycle: current_paths is still unreliable. Keep grid empty.
            this._slideshowPostClear = true;
            this._slideshowSelected = new Set();
          }
          if (this._hass) {
            this._hass.callService('mqtt', 'publish', {
              topic: 'frame_tv/cmd/slideshow/available/request',
              payload: JSON.stringify({ req_id: Date.now() }),
              qos: 1, retain: false,
            }).catch(() => {});
          }
        }
        this._renderOverrideGrid();
      } else if (mode === 'override') {
        // Panel is closed but selection was resynced — update counter badge in place
        this._updateOverrideCounter();
      }
    }
  }

  _handleSlideshowAvailableMessage(message) {
    const payload = this._parseJsonPayload(message);
    this._slideshowAvailable = (payload && Array.isArray(payload.images)) ? payload.images : [];
    this._preloadThumbnails(this._slideshowAvailable);
    if (this._overridePanelOpen) this._renderOverrideGrid();
  }

  _handleSyncAckMessage(message) {
    const payload = this._parseJsonPayload(message);
    const status = String((payload && payload.status) || '').toLowerCase();
    const messageText = String((payload && payload.message) || '').trim();
    const reqId = payload && payload.req_id != null ? String(payload.req_id) : '';
    this._syncAck = {
      status,
      message: messageText,
      req_id: reqId,
      updated: Date.now(),
    };
    if (typeof this._setStatus !== 'function') return;
    if (status === 'started') {
      this._setStatus(messageText || 'Collections sync started...', 0);
    } else if (status === 'ok') {
      this._setStatus(messageText || 'Collections sync completed.', 10000);
    } else if (status === 'error') {
      this._setStatus(messageText ? `Collections sync failed: ${messageText}` : 'Collections sync failed.', 12000);
    }
  }

  _getState(entityId) {
    if (!this._hass || !this._hass.states[entityId]) return '';
    return this._hass.states[entityId].state;
  }

  _getAttrs(entityId) {
    if (!this._hass || !this._hass.states[entityId]) return {};
    return this._hass.states[entityId].attributes || {};
  }

  _getOptions(entityId) {
    if (!this._hass || !this._hass.states[entityId]) return [];
    return this._hass.states[entityId].attributes.options || [];
  }

  _arraysEqual(a, b) {
    const aa = (a || []).slice().sort();
    const bb = (b || []).slice().sort();
    if (aa.length !== bb.length) return false;
    for (let i = 0; i < aa.length; i++) if (aa[i] !== bb[i]) return false;
    return true;
  }

  _getSelectedCollections() {
    const attrs = this._getAttrs(this._config.selected_collections_entity);
    if (attrs && Array.isArray(attrs.selected_labels)) {
      return attrs.selected_labels.map(s => String(s || '').trim()).filter(s => s.length > 0);
    }
    if (attrs && Array.isArray(attrs.selected_collections)) {
      return attrs.selected_collections.map(s => String(s || '').trim()).filter(s => s.length > 0);
    }
    const raw = this._getState(this._config.selected_collections_entity);
    if (!raw || raw === 'unknown' || raw === 'unavailable' || raw === 'None' || raw === '') {
      return [];
    }
    return raw.split(',').map(s => s.trim()).filter(s => s.length > 0);
  }

  // CSV helpers removed — card relies on MQTT attributes and filename fallback

  _parseArtworkInfo(file) {
    if (!file || file === 'unknown' || file === 'unavailable' || file === 'None' || file === '') {
      return null;
    }

    // Remove file extension
    const raw = file.replace(/\.(jpg|jpeg|png|gif|bmp|webp)$/i, '');

    // Parse details from filename as a fallback
    // Remove file extension if present
    let cleanRaw = raw;
    
    let artist = null;
    let title = cleanRaw;
    let year = null;

    // First try to parse underscore-separated format: Artist_Year_Title
    const underscoreParts = cleanRaw.split('_');
    if (underscoreParts.length >= 3) {
      // Check if second part is a 4-digit year
      const possibleYear = underscoreParts[1];
      if (/^\d{4}$/.test(possibleYear) && parseInt(possibleYear) >= 1000 && parseInt(possibleYear) <= 2100) {
        artist = underscoreParts[0].replace(/_/g, ' ');
        year = possibleYear;
        title = underscoreParts.slice(2).join(' ').replace(/_/g, ' ');
      } else {
        // Not the expected format, treat as title with possible artist
        title = cleanRaw.replace(/_/g, ' ');
      }
    } else {
      // Fallback to space/dash parsing for other formats
      title = cleanRaw;
      
      // Extract year in parentheses
      const yearMatch = title.match(/\((\d{4})\)/);
      if (yearMatch) {
        year = yearMatch[1];
        title = title.replace(/\s*\(\d{4}\)/, '');
      }

      // Extract artist if there's a separator
      const artistMatch = title.match(/^(.+?)\s*[-–—]\s*(.+)$/);
      if (artistMatch) {
        artist = artistMatch[1].trim();
        title = artistMatch[2].trim();
      }
    }

    return {
      artist: artist,
      title: title,
      year: year
    };
  }

  _getSelectedData() {
    // Try to read a JSON-packed state from the configured entity.
    // Fallback to treating state as a plain filename.
    const entityId = this._config.selected_artwork_file_entity;
    const raw = this._getState(entityId);
    const attrs = this._getAttrs(entityId);

    // Preferred: attributes provided by an MQTT sensor (file, collection, display)
    if (attrs && (attrs.file || attrs.display || attrs.collection)) {
      return {
        file: attrs.file || '',
        display: attrs.display || null,
        collection: attrs.collection || null,
      };
    }
    if (!raw || raw === 'unknown' || raw === 'unavailable') {
      return { file: '', display: null, collection: null };
    }
    const trimmed = (raw || '').trim();
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
      try {
        const obj = JSON.parse(trimmed);
        return {
          file: obj.file || '',
          display: obj.display || null,
          collection: obj.collection || null,
        };
      } catch (_) {
        // fall through to plain string
      }
    }
    return { file: trimmed, display: null, collection: null };
  }

  _escapeHtml(text) {
    if (text == null) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  _formatInline(text) {
    // Escape first to prevent injection, then apply simple inline formatting
    let s = this._escapeHtml(text);
    // Bold: **text** or *text*
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*(.+?)\*/g, '<strong>$1</strong>');
    // Italic: _text_
    s = s.replace(/_(.+?)_/g, '<em>$1</em>');
    // Inline code: `code`
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    return s;
  }

  _formatMultiline(text) {
    const s = this._formatInline(text);
    return s.replace(/\r?\n/g, '<br>');
  }

  async _updateArtworkText(file, _, isStandby) {
    // Don't overwrite progress messages while a refresh is running
    if (this._refreshInProgress) return;
    try {
      let artworkText;
      const entityId = this._config.selected_artwork_file_entity;
      const attrs = this._getAttrs(entityId);
      const normalizedFile = String(file || '').trim().toLowerCase();
      const standbyLike = isStandby || !normalizedFile || normalizedFile === 'unknown' || normalizedFile === 'unavailable' || normalizedFile === 'none';
      
      if (standbyLike) {
        artworkText = 'Please stand by as artwork is loaded...';
      } else {
        // Prefer MQTT attributes if provided by the sensor
        const title = attrs.artwork_title || null;
        const year = attrs.artwork_year || null;
        const artist = attrs.artist_name || null;
        const lifespan = attrs.artist_lifespan || null;
        const medium = attrs.artwork_medium || attrs.artist_medium || null;
        const descriptionRaw = attrs.artwork_description;
        const description = (typeof descriptionRaw === 'string') ? descriptionRaw.trim() : '';
        const hasDescription = !!(description && !/^\s*(null|none|n\/a)\s*$/i.test(description) && description !== (file || ''));
        if (title || artist || year || lifespan || medium || hasDescription) {
          const titleText = title || file || 'Selected Artwork';
          // Top: artist_name (artist_lifespan)
          const topLine = (artist || lifespan) ? `
            <div style="line-height:1.3; margin-top: 2px;">
              ${artist ? `<span style=\"font-size:1.1em; font-weight:bold; color: white;\">${this._formatInline(artist)}</span>` : ''}
              ${lifespan ? `<span style=\"font-size:0.9em; color: rgba(255,255,255,0.7);\"> (${lifespan})</span>` : ''}
            </div>
          ` : '';
          // Middle: artwork_title, artwork_year (title italic, year lighter)
          const middleLine = `
            <div style="line-height:1.3; margin-top: 8px;">
              <em style="font-size:1.1em; color: white;">${this._formatInline(titleText)}</em>
              ${year ? `<span style=\"font-size:0.9em; color: rgba(255,255,255,0.7);\">, ${year}</span>` : ''}
            </div>
          `;
          // Bottom: artwork_medium
          const bottomLine = medium ? `
            <div style="font-size:0.9em; color: rgba(255,255,255,0.7); line-height:1.4; margin-top: 4px;">${this._formatInline(medium)}</div>
          ` : '';
          artworkText = `${topLine}${middleLine}${bottomLine}`;
          if (hasDescription) {
            artworkText += `<hr style="border: none; border-top: 1px solid rgba(255,255,255,0.3); margin: 8px 0; width: calc(100% + 20px); margin-left: -10px;"><div style="text-align: justify; color: rgba(255,255,255,0.7); font-size: 0.9em; line-height: 1.4;">${this._formatMultiline(description)}</div>`;
          }
        } else {
          // Fallback to filename parsing (no lifespan/medium available)
          const displayInfo = this._parseArtworkInfo(file);
          const fTitle = displayInfo?.title || file || 'Selected Artwork';
          const fYear = displayInfo?.year || null;
          const fArtist = displayInfo?.artist || null;
          const topLine = fArtist ? `
            <div style="line-height:1.3; margin-top: 2px;">
              <span style="font-size:1.1em; font-weight:bold; color: white;">${this._formatInline(fArtist)}</span>
            </div>
          ` : '';
          const middleLine = `
            <div style="line-height:1.3; margin-top: 8px;">
              <em style="font-size:1.1em; color: white;">${this._formatInline(fTitle)}</em>
              ${fYear ? `<span style=\"font-size:0.9em; color: rgba(255,255,255,0.7);\">, ${fYear}</span>` : ''}
            </div>
          `;
          artworkText = `${topLine}${middleLine}`;
        }
      }

      // Update only the info text without re-rendering everything
      const infoDiv = this.querySelector('.ftv-info');
      if (infoDiv) {
        infoDiv.innerHTML = artworkText;
      }
      
      // Also update background if we now have metadata
      this._updateBackgroundFromCsv();
    } catch (error) {
      console.warn('Error updating artwork text:', error);
      // Fallback to filename parsing on error
      const fallbackInfo = this._parseArtworkInfo(file);
      const fTitle = fallbackInfo?.title || file || 'Selected Artwork';
      const fYear = fallbackInfo?.year || null;
      const fArtist = fallbackInfo?.artist || null;
      const normalizedFile = String(file || '').trim().toLowerCase();
      const standbyLike = isStandby || !normalizedFile || normalizedFile === 'unknown' || normalizedFile === 'unavailable' || normalizedFile === 'none';
      const artworkText = standbyLike 
        ? 'Please stand by as artwork is loaded...'
        : `
        ${fArtist ? `<div style=\"line-height:1.3; margin-top: 2px;\"><span style=\"font-size:1.1em; font-weight:bold; color: white;\">${this._formatInline(fArtist)}</span></div>` : ''}
            <div style=\"line-height:1.3; margin-top: 8px;\"><em style=\"font-size:1.1em; color: white;\">${this._formatInline(fTitle)}</em>${fYear ? `<span style=\"font-size:0.9em; color: rgba(255,255,255,0.7);\">, ${fYear}</span>` : ''}</div>
          `;
      
      const infoDiv = this.querySelector('.ftv-info');
      if (infoDiv) {
        infoDiv.innerHTML = artworkText;
      }
    }
  }

  _updateBackgroundFromCsv() {
    const { file } = this._getSelectedData();
    const normalizedFile = String(file || '').trim().toLowerCase();
    // Skip standby / missing / sentinel values — background is already set by _render()
    if (!normalizedFile || normalizedFile === 'standby.png' || normalizedFile === 'unknown' || normalizedFile === 'unavailable' || normalizedFile === 'none') {
      return;
    }
    // Reuse _getBackgroundUrl() to avoid duplicating folder-resolution logic
    const bgUrl = this._getBackgroundUrl();
    if (bgUrl) {
      
      // Update the card background directly
      const cardDiv = this.querySelector('.ftv-card');
      if (cardDiv) {
        // Log the computed URL for debugging (network/CORS/mixed-content issues)
        try { console.info('FRAME-TV-ART-CARD: computed bgUrl ->', bgUrl); } catch (e) {}
        // Detect likely mixed-content (http image on https page)
        if (typeof window !== 'undefined' && bgUrl.startsWith('http:') && window.location && window.location.protocol === 'https:') {
          const infoDiv = this.querySelector('.ftv-info');
          if (infoDiv) {
            infoDiv.innerHTML = (infoDiv.innerHTML || '') + '<div style="color: #ffcc00; margin-top:8px; font-size:0.9em;">Warning: image URL uses http: and may be blocked by browser mixed-content policy.</div>';
          }
          try { console.warn('FRAME-TV-ART-CARD: image URL uses http on https page — likely blocked by mixed-content'); } catch (e) {}
        }
        cardDiv.style.background = `linear-gradient(rgba(0,0,0,0.1), rgba(0,0,0,0.1)), url("${bgUrl}")`;
        cardDiv.style.backgroundSize = 'cover';
        cardDiv.style.backgroundPosition = 'center';
        
        // Also update text colors for dark background
        const headerDiv = this.querySelector('.ftv-header');
        if (headerDiv) headerDiv.style.color = 'white';
        
        const controlsDiv = this.querySelector('.ftv-controls');
        if (controlsDiv) controlsDiv.style.color = 'white';
        
        const infoDiv = this.querySelector('.ftv-info');
        if (infoDiv) {
          infoDiv.style.background = 'rgba(0,0,0,0.5)';
          infoDiv.style.color = 'white';
        }
      }
    }
  }

  _callService(domain, service, data) {
    if (this._hass) this._hass.callService(domain, service, data);
  }

  _getBackgroundUrl() {
    const entityId = this._config.selected_artwork_file_entity;
    const { file } = this._getSelectedData();
    const attrs = this._getAttrs(entityId);
    if (!file || file === 'unknown' || file === 'unavailable' || file === 'None' || file === '' || file === 'standby.png') {
      // Return configured standby path if present, else use protocol-appropriate standby under base
      if (this._config.standby_image_path) return this._config.standby_image_path;
      return `${this._getBaseImagePath()}/standby.png`;
    }

    // Prefer explicit artwork_dir from attributes, then collection, then artist_name; else fallback to filename prefix
    if (attrs && attrs.artwork_dir) {
      return `${this._getBaseImagePath()}/${encodeURIComponent(attrs.artwork_dir)}/${encodeURIComponent(file)}`;
    }
    if (attrs && attrs.collection) {
      return `${this._getBaseImagePath()}/${encodeURIComponent(attrs.collection)}/${encodeURIComponent(file)}`;
    }
    if (attrs && attrs.artist_name) {
      return `${this._getBaseImagePath()}/${encodeURIComponent(attrs.artist_name)}/${encodeURIComponent(file)}`;
    }
    const match = file.match(/^(.+?)_[^_]+_/);
    if (match) {
      const collection = match[1];
      return `${this._getBaseImagePath()}/${encodeURIComponent(collection)}/${encodeURIComponent(file)}`;
    }
    
    return null;
  }
  

  _render() {
    if (!this._hass) return;
    const { file } = this._getSelectedData();
    const normalizedFile = String(file || '').trim().toLowerCase();
    const selectedCollections = this._baselineSelected || this._getSelectedCollections();
    const options = this._getOptions(this._config.collections_entity).filter(opt => opt !== '@eaDir');
    const selectedOptions = options.filter(opt => selectedCollections.includes(opt)).sort();
    const unselectedOptions = options.filter(opt => !selectedCollections.includes(opt)).sort();
    const sortedOptions = [...selectedOptions, ...unselectedOptions];
    const artworkInfo = this._parseArtworkInfo(file);
    // While a refresh is in progress, force standby display regardless of HA state
    const standbyBgUrl = this._config.standby_image_path || `${this._getBaseImagePath()}/standby.png`;
    const bgUrl = this._refreshInProgress ? standbyBgUrl : this._getBackgroundUrl();
    const isStandby = this._refreshInProgress || !normalizedFile || normalizedFile === 'standby.png' || normalizedFile === 'unknown' || normalizedFile === 'unavailable' || normalizedFile === 'none';
    const hasArtwork = bgUrl !== null;

    // Initialize staged selection to baseline on render
    this._currentSelected = Array.isArray(this._currentSelected) && this._dropdownOpen
      ? this._currentSelected
      : [...selectedCollections];

    const selectedText = this._currentSelected.length > 0 
      ? 'Selected: ' + this._currentSelected.join(', ')
      : 'Select collections...';

    // Initial placeholder - will be updated asynchronously by _updateArtworkText
    let artworkText = 'Loading...';

    // Update artwork text — skipped when refresh is in progress (progress msg shown instead)
    if (this._refreshInProgress) {
      setTimeout(() => {
        const infoEl = this.querySelector('.ftv-info');
        const logEl = this.querySelector('.ftv-refresh-log');
        if (infoEl) infoEl.innerHTML = this._refreshProgressLog.length ? `<div>${this._refreshProgressLog[0]}</div>` : '';
        if (logEl) logEl.innerHTML = this._refreshProgressLog.slice(1).map(l => `<div>${l}</div>`).join('');
      }, 0);
    } else {
      setTimeout(() => {
        this._updateArtworkText(file, file, isStandby);
      }, 100);
    }

    this.innerHTML = `
      <ha-card>
        <style>
          ha-card {
            overflow: visible;
          }
          .ftv-card {
            padding: 12px;
            position: relative;
            border-radius: var(--ha-card-border-radius, 12px);
            ${hasArtwork ? `background: linear-gradient(rgba(0,0,0,0.1), rgba(0,0,0,0.1)), url("${bgUrl}"); background-size: cover; background-position: center;` : ''}
          }
          .ftv-header {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 0 0 8px 0;
            margin-bottom: 12px;
            ${hasArtwork ? 'color: white;' : ''}
          }
          .ftv-header .spacer { flex: 1; }
          .ftv-gear {
            margin-left: auto;
            background: transparent;
            border: none;
            cursor: pointer;
            color: inherit;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 32px; height: 32px;
            border-radius: 6px;
          }
          .ftv-gear:hover { background: rgba(255,255,255,0.12); }
          .ftv-settings {
            display: none;
            position: absolute;
            right: 12px;
            top: 56px;
            background: rgba(30,30,30,0.98);
            border: 1px solid var(--divider-color, #444);
            border-radius: 8px;
            padding: 12px;
            z-index: 10000;
            color: #f0f0f0;
            width: 280px;
            box-shadow: 0 6px 18px rgba(0,0,0,0.4);
          }
          .ftv-settings.open { display: block; }
          .ftv-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
          .ftv-label { font-size: 0.85em; color: rgba(255,255,255,0.7); }
          .ftv-input { padding: 8px; border: 1px solid #555; background: #222; color: #fff; border-radius: 6px; }
          .ftv-settings .actions { display: flex; flex-direction: row; gap: 8px; }
          .ftv-btn { padding: 10px 12px; border: none; border-radius: 6px; cursor: pointer; width: auto; box-sizing: border-box; flex: 1 1 50%; }
          .ftv-btn.primary { background: #2f7fbf; color: #fff; }
          .ftv-btn.ghost { background: transparent; color: #fff; border: 1px solid #555; }
          .ftv-icon-wrap {
            width: 42px;
            height: 42px;
            border-radius: 50%;
            background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.2);
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
          }
          .ftv-icon-wrap ha-icon {
            --mdc-icon-size: 24px;
            color: var(--primary-color, #03a9f4);
          }
          .ftv-controls {
            border-radius: 8px;
            margin-bottom: 8px;
            ${hasArtwork ? 'color: white;' : ''}
          }
          .ftv-row {
            display: flex;
            gap: 8px;
          }
          .ftv-dropdown-wrap {
            flex: 1;
            position: relative;
            min-width: 0;
          }
          .ftv-trigger {
            width: 100%;
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid rgba(255,255,255,0.3);
            background: ${hasArtwork ? 'rgba(0,0,0,0.5)' : 'var(--input-fill-color, #f5f5f5)'};
            color: inherit;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-sizing: border-box;
          }
          .ftv-trigger:hover {
            border-color: var(--primary-color, #03a9f4);
          }
          .ftv-trigger-text {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
          }
          .ftv-trigger-arrow {
            margin-left: 8px;
            transition: transform 0.2s;
          }
          .ftv-trigger.open .ftv-trigger-arrow {
            transform: rotate(180deg);
          }
          .ftv-dropdown {
            display: none;
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            margin-top: 4px;
            background: rgba(30,30,30,0.95);
            border: 1px solid var(--divider-color, #ccc);
            border-radius: 8px;
            max-height: 250px;
            overflow-y: auto;
            z-index: 9999;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            color: white;
          }
          .ftv-controls {
            position: relative;
          }
          .ftv-dropdown.open {
            display: block;
          }
          .ftv-option {
            display: flex;
            align-items: center;
            padding: 10px 12px;
            cursor: pointer;
            gap: 10px;
          }
          .ftv-option:hover {
            background: rgba(3, 169, 244, 0.1);
          }
          .ftv-option.selected {
            background: rgba(3, 169, 244, 0.15);
          }
          .ftv-checkbox {
            width: 20px;
            height: 20px;
            border: 2px solid #ccc;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .ftv-option.selected .ftv-checkbox {
            background: var(--primary-color, #03a9f4);
            border-color: var(--primary-color, #03a9f4);
          }
          .ftv-checkbox svg {
            width: 14px;
            height: 14px;
            fill: white;
            opacity: 0;
          }
          .ftv-option.selected .ftv-checkbox svg {
            opacity: 1;
          }
          .ftv-clear {
            width: 40px;
            height: 40px;
            border: none;
            border-radius: 8px;
            background: #db4437;
            color: white;
            cursor: pointer;
            display: ${selectedCollections.length > 0 ? 'flex' : 'none'};
            align-items: center;
            justify-content: center;
          }
          .ftv-progress-wrap {
            ${hasArtwork ? 'background: rgba(0,0,0,0.5); border-radius: 8px; overflow: hidden;' : ''}
          }
          .ftv-info {
            display: block;
            width: 100%;
            box-sizing: border-box;
            padding: 12px;
            ${hasArtwork ? 'color: white;' : ''}
          }
          .ftv-refresh-log {
            font-size: 0.85em;
            line-height: 1.6;
            ${hasArtwork ? 'padding: 0 12px 10px; color: rgba(255,255,255,0.75);' : 'color: var(--secondary-text-color);'}
          }
          .ftv-refresh-log:empty { display: none; }
          .ftv-status {
            margin-top: 0;
            min-height: 0;
            font-size: 0.85em;
            color: ${hasArtwork ? 'rgba(255,255,255,0.6)' : 'var(--secondary-text-color)'};
          }
          .ftv-apply {
            width: 40px;
            height: 40px;
            border: none;
            border-radius: 8px;
            background: var(--primary-color, #03a9f4);
            color: white;
            cursor: pointer;
            display: none;
            align-items: center;
            justify-content: center;
          }
          .ftv-refresh {
            width: 40px;
            height: 40px;
            border: none;
            border-radius: 8px;
            background: #4a6fa5;
            color: white;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .ftv-apply[disabled] {
            opacity: 0.5;
            cursor: default;
          }
          .ftv-override-badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 3px 9px;
            border-radius: 12px;
            font-size: 0.75em;
            font-weight: 600;
            background: rgba(255, 180, 0, 0.22);
            color: #ffb400;
            border: 1px solid rgba(255, 180, 0, 0.45);
            cursor: pointer;
            white-space: nowrap;
            user-select: none;
          }
          .ftv-override-badge:hover {
            background: rgba(255, 180, 0, 0.35);
          }
          .ftv-override-badge .ftv-badge-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #ffb400;
            flex-shrink: 0;
          }
          .ftv-grid-btn {
            background: transparent;
            border: none;
            cursor: pointer;
            color: inherit;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 32px; height: 32px;
            border-radius: 6px;
          }
          .ftv-grid-btn:hover { background: rgba(255,255,255,0.12); }
          .ftv-grid-btn.active { color: var(--primary-color, #03a9f4); }
          .ftv-override-popup {
            display: none;
            position: absolute;
            left: 0; right: 0; top: 58px;
            background: rgba(18,18,18,0.97);
            border: 1px solid #555;
            border-radius: 0 0 10px 10px;
            z-index: 10001;
            max-height: 65vh;
            overflow-y: auto;
            color: #f0f0f0;
            box-shadow: 0 8px 24px rgba(0,0,0,0.55);
          }
          .ftv-override-popup.open { display: block; }
          .ftv-op-topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 14px 6px;
            border-bottom: 1px solid #333;
          }
          .ftv-op-title { font-weight: 600; font-size: 0.95em; }
          .ftv-op-counter { font-size: 0.8em; color: rgba(255,255,255,0.55); }
          .ftv-op-warn { padding: 6px 14px 0; font-size: 0.8em; color: #ffb400; }
          .ftv-op-actions { display: flex; gap: 8px; padding: 8px 14px; }
          .ftv-op-btn { padding: 6px 14px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.85em; font-weight: 600; }
          .ftv-op-btn.primary { background: #2f7fbf; color: #fff; }
          .ftv-op-btn.danger { background: #c0392b; color: #fff; }
          .ftv-op-btn:disabled { opacity: 0.4; cursor: default; }
          .ftv-op-grid {
            padding: 4px 10px 12px;
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
            gap: 6px;
          }
          .ftv-op-section {
            grid-column: 1 / -1;
            font-size: 0.72em;
            font-weight: 700;
            color: rgba(255,255,255,0.38);
            padding: 6px 2px 2px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
          }
          .ftv-op-thumb {
            position: relative;
            border-radius: 5px;
            border: 2px solid transparent;
            overflow: hidden;
            cursor: pointer;
            background: #222;
          }
          .ftv-op-thumb img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
          .ftv-op-thumb.selected { border-color: #2f7fbf; }
          .ftv-op-thumb.disabled { opacity: 0.38; cursor: not-allowed; }
          .ftv-op-check {
            position: absolute; top: 3px; right: 3px;
            width: 16px; height: 16px;
            border-radius: 3px;
            border: 1.5px solid rgba(255,255,255,0.55);
            background: transparent;
            display: flex; align-items: center; justify-content: center;
            font-size: 10px; color: #fff;
          }
          .ftv-op-thumb.selected .ftv-op-check { background: #2f7fbf; border-color: #2f7fbf; }
          .ftv-op-hint { padding: 16px 14px; font-size: 0.85em; color: rgba(255,255,255,0.4); text-align: center; }
          .ftv-op-switch { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
          .ftv-op-switch input { opacity: 0; width: 0; height: 0; }
          .ftv-op-slider { position: absolute; cursor: pointer; inset: 0; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.2); border-radius: 24px; transition: background 0.2s; }
          .ftv-op-slider::before { content: ""; position: absolute; width: 18px; height: 18px; left: 2px; top: 2px; background: rgba(255,255,255,0.5); border-radius: 50%; transition: transform 0.2s, background 0.2s; }
          .ftv-op-switch input:checked + .ftv-op-slider { background: #2f7fbf; border-color: #2f7fbf; }
          .ftv-op-switch input:checked + .ftv-op-slider::before { transform: translateX(20px); background: #fff; }
          .ftv-op-switch input:disabled + .ftv-op-slider { opacity: 0.4; cursor: not-allowed; }
          .ftv-op-settings { padding: 10px 14px 8px; border-bottom: 1px solid #333; display: flex; flex-direction: column; gap: 8px; }
          .ftv-op-settings-row { display: flex; gap: 6px; align-items: center; }
          .ftv-op-input { flex: 1; padding: 7px 8px; background: #2a2a2a; color: #f0f0f0; border: 1px solid #555; border-radius: 6px; font-size: 0.85em; min-width: 0; }
          .ftv-op-select { flex: 1.2; padding: 7px 8px; background: #2a2a2a; color: #f0f0f0; border: 1px solid #555; border-radius: 6px; font-size: 0.85em; }
          .ftv-op-btn.apply { background: #4a6fa5; color: #fff; }
          .ftv-op-toggle-row { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid #333; }
          .ftv-op-toggle-label { font-size: 0.9em; color: #f0f0f0; }
          .ftv-op-hint-text { font-size: 0.77em; color: rgba(255,255,255,0.38); padding: 0 14px 4px; }
        </style>
        <div class="ftv-card">
          <div class="ftv-header">
            <div class="ftv-icon-wrap">
              <ha-icon icon="${this._config.icon}"></ha-icon>
            </div>
            <span>${this._config.title}</span>
            <span class="spacer"></span>
            ${this._slideshowMode === 'override' ? `<button class="ftv-override-badge" id="ftv-override-badge" title="Override active — click to clear"><span class="ftv-badge-dot"></span>Override</button>` : ''}
            <button class="ftv-grid-btn${this._overridePanelOpen ? ' active' : ''}" id="ftv-grid-btn" title="Slideshow override">
              <ha-icon icon="mdi:view-grid"></ha-icon>
            </button>
            <button class="ftv-gear" id="ftv-gear" title="Settings">
              <ha-icon icon="mdi:cog"></ha-icon>
            </button>
          </div>
          <div class="ftv-override-popup${this._overridePanelOpen ? ' open' : ''}" id="ftv-override-popup">
            <div class="ftv-op-settings">
              <div class="ftv-op-settings-row">
                <select class="ftv-op-select" id="ftv-op-type">
                  <option value="random"${!this._slideshowSeq ? ' selected' : ''}>Random</option>
                  <option value="sequential"${this._slideshowSeq ? ' selected' : ''}>Sequential</option>
                </select>
                <input class="ftv-op-input" id="ftv-op-interval" type="number" min="0" placeholder="Interval (min)" value="${this._slideshowUpdateMins}" />
                <input class="ftv-op-input" id="ftv-op-max" type="number" min="1" placeholder="Max uploads" value="${this._slideshowMaxUploads}" />
                <button class="ftv-op-btn apply" id="ftv-op-settings-apply" disabled>Apply</button>
              </div>
              <div class="ftv-op-hint-text">Interval: minutes between changes (0=off). Max: artwork slots on TV.</div>
            </div>
            ${this._slideshowMode === 'override' ? '<div class="ftv-op-hint-text" style="margin-top:2px;color:#ffb400;">&#x25CF; Override active</div>' : ''}
            <div class="ftv-op-topbar">
              <span class="ftv-op-counter" id="ftv-op-counter">0 / ${this._slideshowMaxUploads} selected</span>
              <div style="display:flex;gap:6px;">
                <button class="ftv-op-btn" id="ftv-op-reset" style="display:none;">Reset</button>
                <button class="ftv-op-btn primary" id="ftv-op-apply" disabled>${this._slideshowMode === 'override' ? 'Update Override' : 'Apply Override'}</button>
                <button class="ftv-op-btn danger" id="ftv-op-clear" style="${this._slideshowMode !== 'override' ? 'display:none' : ''}">Clear Override</button>
              </div>
            </div>
            ${this._slideshowMode === 'override' ? '<div class="ftv-op-hint-text" style="margin-bottom:2px;">&#x2713; Override active — re-apply anytime to change the selection.</div>' : ''}
            <div id="ftv-op-warn" class="ftv-op-warn" style="${this._slideshowUploading ? '' : 'display:none'}">&#9888; Uploading — grid locked</div>
            <div class="ftv-op-grid" id="ftv-op-grid"></div>
          </div>
          <div class="ftv-controls">
            <div class="ftv-row">
                <div class="ftv-dropdown-wrap">
                  <div class="ftv-trigger" id="ftv-trigger">
                    <span class="ftv-trigger-text">${selectedText}</span>
                    <span class="ftv-trigger-arrow">▼</span>
                  </div>
                  <div class="ftv-dropdown" id="ftv-dropdown">
                    ${sortedOptions.map(opt => `
                      <div class="ftv-option ${this._currentSelected.includes(opt) ? 'selected' : ''}" data-value="${opt}">
                        <div class="ftv-checkbox">
                          <svg viewBox="0 0 24 24"><path d="M9,20.42L2.79,14.21L5.62,11.38L9,14.77L18.88,4.88L21.71,7.71L9,20.42Z"/></svg>
                        </div>
                        <span>${opt}</span>
                      </div>
                    `).join('')}
                  </div>
                </div>
                <button class="ftv-refresh" id="ftv-refresh" title="Refresh uploads">
                  <ha-icon icon="mdi:refresh"></ha-icon>
                </button>
                <button class="ftv-apply" id="ftv-apply" title="Apply selections">
                  <ha-icon icon="mdi:check"></ha-icon>
                </button>
                <button class="ftv-clear" id="ftv-clear">
                  <ha-icon icon="mdi:delete"></ha-icon>
                </button>
              </div>
              <div class="ftv-status" id="ftv-status">${this._statusMessage || ''}</div>
          </div>
          <div class="ftv-progress-wrap">
            <div class="ftv-info">${artworkText}</div>
            <div class="ftv-refresh-log"></div>
          </div>
          <div class="ftv-settings" id="ftv-settings">
            <div class="ftv-field">
              <div class="ftv-label">Frame TV IP address</div>
              <input class="ftv-input" id="ftv-tv-ip" type="text" placeholder="e.g. 10.83.21.57" />
            </div>
            <div class="ftv-field">
              <div class="ftv-label">MQTT broker host</div>
              <input class="ftv-input" id="ftv-mqtt-host" type="text" placeholder="e.g. mosquitto" />
            </div>
            <div class="ftv-field">
              <div class="ftv-label">MQTT port</div>
              <input class="ftv-input" id="ftv-mqtt-port" type="number" placeholder="e.g. 1883" />
            </div>
            <div class="ftv-field">
              <div class="ftv-label">MQTT username</div>
              <input class="ftv-input" id="ftv-mqtt-user" type="text" placeholder="(optional)" />
            </div>
            <div class="ftv-field">
              <div class="ftv-label">MQTT password</div>
              <input class="ftv-input" id="ftv-mqtt-pass" type="password" placeholder="(optional)" />
            </div>
            <div class="actions">
              <button class="ftv-btn primary" id="ftv-apply-env" disabled>Apply &amp; Restart</button>
              <button class="ftv-btn ghost" id="ftv-restart-env">Restart Uploader</button>
              <button class="ftv-btn ghost" id="ftv-sync-collections">Update &amp; Refresh</button>
            </div>
            <div class="ftv-label" id="ftv-env-msg" style="margin-top:6px;"></div>
          </div>
        </div>
      </ha-card>
    `;
    // Settings panel logic
    const gear = this.querySelector('#ftv-gear');
    const panel = this.querySelector('#ftv-settings');
    const inIp = this.querySelector('#ftv-tv-ip');
    const inMqttHost = this.querySelector('#ftv-mqtt-host');
    const inMqttPort = this.querySelector('#ftv-mqtt-port');
    const inMqttUser = this.querySelector('#ftv-mqtt-user');
    const inMqttPass = this.querySelector('#ftv-mqtt-pass');
    const btnApplyEnv = this.querySelector('#ftv-apply-env');
    const btnRestartEnv = this.querySelector('#ftv-restart-env');
    const btnSyncCollections = this.querySelector('#ftv-sync-collections');
    const btnRefresh = this.querySelector('#ftv-refresh');
    const statusEl = this.querySelector('#ftv-status');
    const envMsg = this.querySelector('#ftv-env-msg');
    const setStatus = (msg = '', timeoutMs = 6000) => {
      this._statusMessage = msg || '';
      if (statusEl) statusEl.textContent = this._statusMessage;
      if (timeoutMs > 0 && msg) {
        setTimeout(() => {
          if (this._statusMessage === msg) {
            this._statusMessage = '';
            if (statusEl) statusEl.textContent = '';
          }
        }, timeoutMs);
      }
    };
    this._setStatus = setStatus;
    this._syncRefreshAckStatus();
    const settingsEntity = this._config.settings_entity;
    let envBaseline = {};
    function envDirty() {
      const changed = (
        String(inIp?.value||'') !== String(envBaseline.SAMSUNG_TV_ART_TV_IP||'') ||
        String(inMqttHost?.value||'') !== String(envBaseline.SAMSUNG_TV_ART_MQTT_HOST||'') ||
        String(inMqttPort?.value||'') !== String(envBaseline.SAMSUNG_TV_ART_MQTT_PORT||'') ||
        String(inMqttUser?.value||'') !== String(envBaseline.SAMSUNG_TV_ART_MQTT_USERNAME||'') ||
        (inMqttPass?.value||'').length > 0
      );
      if (btnApplyEnv) { btnApplyEnv.disabled = !changed; }
    }
    const loadEnv = () => {
      try {
        if (!settingsEntity || !this._hass) return;
        const st = this._hass.states[settingsEntity];
        const attrs = (st && st.attributes) || {};
        envBaseline = {
          SAMSUNG_TV_ART_TV_IP: attrs.SAMSUNG_TV_ART_TV_IP || '',
          SAMSUNG_TV_ART_MQTT_HOST: attrs.SAMSUNG_TV_ART_MQTT_HOST || '',
          SAMSUNG_TV_ART_MQTT_PORT: attrs.SAMSUNG_TV_ART_MQTT_PORT || '',
          SAMSUNG_TV_ART_MQTT_USERNAME: attrs.SAMSUNG_TV_ART_MQTT_USERNAME || '',
        };
        if (inIp) inIp.value = envBaseline.SAMSUNG_TV_ART_TV_IP;
        if (inMqttHost) inMqttHost.value = envBaseline.SAMSUNG_TV_ART_MQTT_HOST;
        if (inMqttPort) inMqttPort.value = envBaseline.SAMSUNG_TV_ART_MQTT_PORT;
        if (inMqttUser) inMqttUser.value = envBaseline.SAMSUNG_TV_ART_MQTT_USERNAME;
        envDirty();
      } catch (_) {}
    };
    // Grid (override) button — toggle the override popup
    // The amber Override badge also opens the panel on click
    const gridBtn = this.querySelector('#ftv-grid-btn');
    const overrideBadge = this.querySelector('#ftv-override-badge');
    const openOverridePanel = () => {
      if (!this._overridePanelOpen) {
        this._overridePanelOpen = true;
        // Only seed selection from paths when not in post-clear state
        // (post-clear: current_paths is stale — user should pick fresh images)
        if (!this._slideshowPostClear && this._slideshowSelected.size === 0) {
          this._slideshowSelected = new Set(
            this._slideshowOverridePaths.length ? this._slideshowOverridePaths : this._slideshowCurrentPaths
          );
        }
        // Direct DOM toggle — avoids full re-render and background flash
        const _pop = this.querySelector('#ftv-override-popup');
        const _gbtn = this.querySelector('#ftv-grid-btn');
        if (_pop) _pop.classList.add('open');
        if (_gbtn) _gbtn.classList.add('active');
        // Request a fresh available list so grid populates
        if (this._hass) {
          this._hass.callService('mqtt', 'publish', {
            topic: 'frame_tv/cmd/slideshow/available/request',
            payload: JSON.stringify({ req_id: Date.now() }),
            qos: 1, retain: false,
          }).catch(() => {});
        }
        this._renderOverrideGrid();
        this._lastStateHash = this._getStateHash();
      }
    };
    if (overrideBadge) {
      overrideBadge.addEventListener('click', (e) => { e.stopPropagation(); openOverridePanel(); });
    }
    if (gridBtn) {
      gridBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._overridePanelOpen = !this._overridePanelOpen;
        if (this._overridePanelOpen && !this._slideshowPostClear && this._slideshowSelected.size === 0) {
          this._slideshowSelected = new Set(
            this._slideshowOverridePaths.length ? this._slideshowOverridePaths : this._slideshowCurrentPaths
          );
        }
        // Direct DOM toggle — avoids full re-render and background flash
        const _pop = this.querySelector('#ftv-override-popup');
        if (_pop) _pop.classList.toggle('open', this._overridePanelOpen);
        gridBtn.classList.toggle('active', this._overridePanelOpen);
        if (this._overridePanelOpen) {
          // Request a fresh available list so grid populates
          if (this._hass) {
            this._hass.callService('mqtt', 'publish', {
              topic: 'frame_tv/cmd/slideshow/available/request',
              payload: JSON.stringify({ req_id: Date.now() }),
              qos: 1, retain: false,
            }).catch(() => {});
          }
          this._renderOverrideGrid();
        }
        this._lastStateHash = this._getStateHash();
      });
    }
    // Override popup: Reset
    const opReset = this.querySelector('#ftv-op-reset');
    if (opReset) {
      opReset.addEventListener('click', (e) => {
        e.stopPropagation();
        const baseline = this._slideshowMode === 'override' ? this._slideshowOverridePaths : this._slideshowCurrentPaths;
        this._slideshowSelected = new Set(baseline);
        this._renderOverrideGrid();
      });
    }
    // Override popup: Apply
    const opApply = this.querySelector('#ftv-op-apply');
    if (opApply) {
      opApply.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!this._hass || !this._slideshowSelected.size) return;
        const paths = Array.from(this._slideshowSelected);
        const origLabel = opApply.textContent;
        opApply.disabled = true;
        opApply.textContent = 'Applying\u2026';
        this._hass.callService('mqtt', 'publish', {
          topic: 'frame_tv/cmd/slideshow/override/set',
          payload: JSON.stringify({ paths, req_id: Date.now() }),
          qos: 1, retain: false,
        }).catch(() => {});
        // Safety fallback only — MQTT response cancels this via _updateOverrideCounter
        this._applyRestoreTimer = setTimeout(() => {
          this._applyRestoreTimer = null;
          if (opApply) {
            opApply.textContent = this._slideshowMode === 'override' ? 'Update Override' : origLabel;
            opApply.disabled = this._slideshowSelected.size === 0 || this._slideshowUploading || this._selectionMatchesBaseline();
          }
        }, 8000);
      });
    }
    // Override popup: Clear
    const opClear = this.querySelector('#ftv-op-clear');
    if (opClear) {
      opClear.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!this._hass) return;
        opClear.disabled = true;
        opClear.textContent = 'Clearing…';
        // Fire a full collections/refresh: clears override, prunes TV storage,
        // and reseeds from current collections — guaranteeing current_paths is fresh.
        this._slideshowClearRefreshPending = true;
        this._hass.callService('mqtt', 'publish', {
          topic: 'frame_tv/cmd/collections/refresh',
          payload: JSON.stringify({ req_id: Date.now() }),
          qos: 1, retain: false,
        }).catch(() => {
          setStatus('Failed to send clear command.', 8000);
        });
        this._clearRestoreTimer = setTimeout(() => { this._clearRestoreTimer = null; if (opClear) { opClear.disabled = false; opClear.textContent = 'Clear Override'; } }, 8000);
      });
    }
    // Slideshow settings (inside the popup)
    const opType = this.querySelector('#ftv-op-type');
    const opInterval = this.querySelector('#ftv-op-interval');
    const opMax = this.querySelector('#ftv-op-max');
    const opSettingsApply = this.querySelector('#ftv-op-settings-apply');
    const slSettingsDirty = () => {
      if (!opSettingsApply) return;
      const changed = (opType && opType.value !== (this._slideshowSeq ? 'sequential' : 'random')) ||
        parseInt(opInterval?.value || 0, 10) !== this._slideshowUpdateMins ||
        parseInt(opMax?.value || 10, 10) !== this._slideshowMaxUploads;
      opSettingsApply.disabled = !changed;
    };
    if (opType) opType.addEventListener('change', slSettingsDirty);
    if (opInterval) opInterval.addEventListener('input', slSettingsDirty);
    if (opMax) opMax.addEventListener('input', slSettingsDirty);
    if (opSettingsApply) opSettingsApply.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!this._hass) return;
      const sequential = opType && opType.value === 'sequential';
      const update_minutes = parseInt(opInterval?.value || 0, 10);
      const max_uploads = parseInt(opMax?.value || 10, 10);
      this._hass.callService('mqtt', 'publish', { topic: 'frame_tv/cmd/slideshow/settings/set', payload: JSON.stringify({ sequential, update_minutes, req_id: Date.now() }), qos: 1, retain: false });
      this._hass.callService('mqtt', 'publish', { topic: 'frame_tv/cmd/settings/set', payload: JSON.stringify({ SAMSUNG_TV_ART_MAX_UPLOADS: String(max_uploads), SAMSUNG_TV_ART_UPDATE_MINUTES: String(update_minutes) }), qos: 1, retain: false });
      if (opSettingsApply) opSettingsApply.disabled = true;
    });
    // Populate the grid immediately if panel is open
    if (this._overridePanelOpen) this._renderOverrideGrid();
    if (gear && panel) {
      gear.addEventListener('click', (e) => {
        e.stopPropagation();
        panel.classList.toggle('open');
        if (panel.classList.contains('open')) loadEnv();
      });
      // Remove previous document-level listener before registering a new one
      if (this._docClickHandler) {
        document.removeEventListener('click', this._docClickHandler, { capture: true });
        this._docClickHandler = null;
      }
      this._docClickHandler = (e) => {
        const path = (typeof e.composedPath === 'function') ? e.composedPath() : [];
        const inside = (panel && path.includes(panel)) || (gear && path.includes(gear));
        if (!inside) panel.classList.remove('open');
        // Also close override popup on outside click
        const overridePopup = this.querySelector('#ftv-override-popup');
        const gridBtnEl = this.querySelector('#ftv-grid-btn');
        const badgeEl = this.querySelector('#ftv-override-badge');
        const insideOverride = (overridePopup && path.includes(overridePopup)) || (gridBtnEl && path.includes(gridBtnEl)) || (badgeEl && path.includes(badgeEl));
        if (!insideOverride && this._overridePanelOpen) {
          this._overridePanelOpen = false;
          // Direct DOM toggle — avoids full re-render and background flash
          if (overridePopup) overridePopup.classList.remove('open');
          if (gridBtnEl) gridBtnEl.classList.remove('active');
          this._lastStateHash = this._getStateHash();
        }
      };
      document.addEventListener('click', this._docClickHandler, { capture: true });
    }
    if (inIp) inIp.addEventListener('input', envDirty);
    if (inMqttHost) inMqttHost.addEventListener('input', envDirty);
    if (inMqttPort) inMqttPort.addEventListener('input', envDirty);
    if (inMqttUser) inMqttUser.addEventListener('input', envDirty);
    if (inMqttPass) inMqttPass.addEventListener('input', envDirty);
    if (btnApplyEnv) btnApplyEnv.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        const payload = {
          SAMSUNG_TV_ART_TV_IP: String(inIp?.value||'').trim(),
          SAMSUNG_TV_ART_MQTT_HOST: String(inMqttHost?.value||'').trim(),
          SAMSUNG_TV_ART_MQTT_PORT: String(inMqttPort?.value||'').trim(),
          SAMSUNG_TV_ART_MQTT_USERNAME: String(inMqttUser?.value||'').trim(),
        };
        if (inMqttPass?.value) payload.SAMSUNG_TV_ART_MQTT_PASSWORD = inMqttPass.value;
        // Publish settings via MQTT (MQTT/IP changes require restart)
        if (this._hass) {
          this._hass.callService('mqtt', 'publish', { topic: 'frame_tv/cmd/settings/set', payload: JSON.stringify(payload), qos: 1, retain: false });
        }
        btnApplyEnv.textContent = 'Applying…';
        btnApplyEnv.disabled = true;
        if (envMsg) envMsg.textContent = 'Settings sent — restart required to apply MQTT/IP changes.';
        const _baselineForReset = { ...payload }; delete _baselineForReset.SAMSUNG_TV_ART_MQTT_PASSWORD;
        setTimeout(() => { btnApplyEnv.textContent = 'Apply & Restart'; envBaseline = _baselineForReset; if (inMqttPass) inMqttPass.value = ''; envDirty(); if (envMsg) envMsg.textContent=''; }, 5000);
      } catch (_) {}
    });

    if (btnRestartEnv) btnRestartEnv.addEventListener('click', (e) => {
      e.stopPropagation();
      try {
        if (this._hass) {
          this._hass.callService('mqtt', 'publish', { topic: 'frame_tv/cmd/settings/restart', payload: JSON.stringify({ req_id: Date.now() }), qos: 1, retain: false });
        }
        if (envMsg) envMsg.textContent = 'Restarting uploader...';
        btnRestartEnv.disabled = true;
        setTimeout(() => { btnRestartEnv.disabled = false; if (envMsg && envMsg.textContent==='Restarting uploader...') envMsg.textContent=''; }, 6000);
      } catch (_) {}
    });

    if (btnSyncCollections) btnSyncCollections.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        const reqId = Date.now();
        this._lastRefreshReqId = reqId;
        this._refreshAck = {
          status: 'queued',
          message: 'Update & refresh requested. Fetching latest collections...',
          req_id: String(reqId),
          updated: Date.now(),
        };
        this._syncRefreshAckStatus();
        if (this._hass) {
          await this._hass.callService('mqtt', 'publish', { topic: 'frame_tv/cmd/settings/sync_collections', payload: JSON.stringify({ req_id: reqId }), qos: 1, retain: false });
        }
        btnSyncCollections.disabled = true;
        setTimeout(() => { btnSyncCollections.disabled = false; }, 6000);
      } catch (err) {
        setStatus('Update & refresh failed to send. Check MQTT integration/service.', 8000);
      }
    });

    if (btnRefresh) btnRefresh.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        const reqId = Date.now();
        this._lastRefreshReqId = reqId;
        this._refreshAck = {
          status: 'queued',
          message: 'Refresh command sent. Waiting for backend confirmation...',
          req_id: String(reqId),
          updated: Date.now(),
        };
        this._syncRefreshAckStatus();
        if (this._hass) {
          await this._hass.callService('mqtt', 'publish', { topic: this._config.refresh_cmd_topic, payload: JSON.stringify({ req_id: reqId }), qos: 1, retain: false });
        }
        if (envMsg) envMsg.textContent = 'Requested collections refresh...';
        btnRefresh.disabled = true;
        setTimeout(() => { btnRefresh.disabled = false; if (envMsg && envMsg.textContent==='Requested collections refresh...') envMsg.textContent=''; }, 6000);
      } catch (err) {
        setStatus('Refresh failed to send. Check MQTT integration/service.', 8000);
      }
    });

    // Poll for applied values as a soft ACK (HA frontend cannot subscribe MQTT directly)
    const pollApplied = () => {
      try {
        if (!panel || !panel.classList.contains('open')) return;
        const st = this._hass && settingsEntity ? this._hass.states[settingsEntity] : null;
        const attrs = (st && st.attributes) || {};
        const ok = (
          String(attrs.SAMSUNG_TV_ART_TV_IP||'') === String(inIp?.value||'') &&
          String(attrs.SAMSUNG_TV_ART_MQTT_HOST||'') === String(inMqttHost?.value||'') &&
          String(attrs.SAMSUNG_TV_ART_MQTT_PORT||'') === String(inMqttPort?.value||'') &&
          String(attrs.SAMSUNG_TV_ART_MQTT_USERNAME||'') === String(inMqttUser?.value||'') &&
          !(inMqttPass?.value||'')
        );
        if (ok && envMsg) { envMsg.textContent = 'Settings applied.'; setTimeout(() => { if (envMsg.textContent==='Settings applied.') envMsg.textContent=''; }, 6000); }
      } catch (_) {}
      this._pollAppliedTimer = setTimeout(pollApplied, 2000);
    };
    if (this._pollAppliedTimer) clearTimeout(this._pollAppliedTimer);
    this._pollAppliedTimer = setTimeout(pollApplied, 2000);

    // Event handlers
    const trigger = this.querySelector('#ftv-trigger');
    const dropdown = this.querySelector('#ftv-dropdown');
    const dropdownWrap = this.querySelector('.ftv-dropdown-wrap');
    
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      this._dropdownOpen = !this._dropdownOpen;
      trigger.classList.toggle('open', this._dropdownOpen);
      dropdown.classList.toggle('open', this._dropdownOpen);
    });

    this.querySelectorAll('.ftv-option').forEach(opt => {
      opt.addEventListener('click', (e) => {
        e.stopPropagation();
        const val = opt.dataset.value;
        const wasSelected = this._currentSelected.includes(val);
        if (wasSelected) {
          this._currentSelected = this._currentSelected.filter(s => s !== val);
        } else {
          this._currentSelected.push(val);
        }
        opt.classList.toggle('selected', !wasSelected);
        // Update trigger text
        const triggerText = this.querySelector('.ftv-trigger-text');
        if (triggerText) {
          triggerText.textContent = this._currentSelected.length > 0 
            ? 'Selected: ' + this._currentSelected.join(', ')
            : 'Select collections...';
        }
        // Toggle clear button visibility dynamically
        const clearBtn = this.querySelector('#ftv-clear');
        if (clearBtn) clearBtn.style.display = this._currentSelected.length > 0 ? 'flex' : 'none';
        // Toggle apply based on diff from baseline
        const applyBtn = this.querySelector('#ftv-apply');
        const changed = !this._arraysEqual(this._currentSelected, this._baselineSelected);
        if (applyBtn) {
          applyBtn.style.display = changed ? 'flex' : 'none';
          applyBtn.disabled = !changed;
        }
      });
    });

    this.querySelector('#ftv-clear').addEventListener('click', (e) => {
      e.stopPropagation();
      // Stage-only clear; apply must be clicked to publish
      this._currentSelected = [];
      const triggerText = this.querySelector('.ftv-trigger-text');
      if (triggerText) triggerText.textContent = 'Select collections...';
      const clearBtn = this.querySelector('#ftv-clear');
      if (clearBtn) clearBtn.style.display = 'none';
      const applyBtn = this.querySelector('#ftv-apply');
      if (applyBtn) {
        const changed = !this._arraysEqual(this._currentSelected, this._baselineSelected);
        applyBtn.style.display = changed ? 'flex' : 'none';
        applyBtn.disabled = !changed;
      }
      this.querySelectorAll('.ftv-option.selected').forEach(o => o.classList.remove('selected'));
    });

    // Apply button publishes staged selections in one update
    const applyBtn = this.querySelector('#ftv-apply');
    if (applyBtn) {
      const changed = !this._arraysEqual(this._currentSelected, this._baselineSelected);
      applyBtn.style.display = changed ? 'flex' : 'none';
      applyBtn.disabled = !changed;
      applyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!this._hass) return;
        const topic = 'frame_tv/cmd/collections/set';
        const payload = { collections: this._currentSelected.slice(), req_id: Date.now() };
        this._hass.callService('mqtt', 'publish', { topic, payload: JSON.stringify(payload) });
        // Optimistically align baseline to staged
        this._baselineSelected = this._currentSelected.slice();
        applyBtn.style.display = 'none';
        applyBtn.disabled = true;
      });
    }

    // Reverted: keep default sizing behavior (background-size: cover) without dynamic min-height

      }

  _renderOverrideGrid() {
    const grid = this.querySelector('#ftv-op-grid');
    if (!grid) return;
    this._updateOverrideCounter();
    if (!this._slideshowAvailable.length) {
      grid.innerHTML = '<div class="ftv-op-hint">No images available. Make sure collections are selected above.</div>';
      return;
    }
    const atMax = this._slideshowSelected.size >= this._slideshowMaxUploads;
    const { file: _curFile } = this._getSelectedData();
    const _standby = !_curFile || ['standby.png','unknown','unavailable','none',''].includes(String(_curFile).trim().toLowerCase());
    const locked = this._slideshowUploading || (_standby && this._slideshowMode !== 'override');
    const basePath = this._getBaseImagePath();
    // Group unselected by artist/folder
    const groups = {};
    for (const img of this._slideshowAvailable) {
      if (this._slideshowSelected.has(img.path)) continue;
      const key = (img.artist && img.artist.trim()) ? img.artist.trim() : (img.folder || 'Unknown');
      if (!groups[key]) groups[key] = [];
      groups[key].push(img);
    }
    let html = '';
    // Selected section first
    const selectedImgs = this._slideshowAvailable.filter(img => this._slideshowSelected.has(img.path));
    if (selectedImgs.length) {
      html += `<div class="ftv-op-section">Selected (${selectedImgs.length})</div>`;
      for (const img of selectedImgs) {
        const url = `${basePath}/${encodeURIComponent(img.folder)}/${encodeURIComponent(img.file)}`;
        html += `<div class="ftv-op-thumb selected${locked ? ' disabled' : ''}" data-path="${this._escapeHtml(img.path)}"><img src="${url}" loading="eager" alt="" onerror="this.style.display='none'"><div class="ftv-op-check">&#10003;</div></div>`;
      }
    }
    // Remaining grouped by artist
    for (const [groupName, images] of Object.entries(groups).sort(([a], [b]) => a.localeCompare(b))) {
      html += `<div class="ftv-op-section">${this._escapeHtml(groupName)}</div>`;
      for (const img of images) {
        const url = `${basePath}/${encodeURIComponent(img.folder)}/${encodeURIComponent(img.file)}`;
        const isDisabled = atMax || locked;
        html += `<div class="ftv-op-thumb${isDisabled ? ' disabled' : ''}" data-path="${this._escapeHtml(img.path)}"><img src="${url}" loading="eager" alt="" onerror="this.style.display='none'"><div class="ftv-op-check"></div></div>`;
      }
    }
    grid.innerHTML = html;
    if (!locked) {
      grid.querySelectorAll('.ftv-op-thumb').forEach(el => {
        el.addEventListener('click', () => {
          if (el.classList.contains('disabled')) return;
          const path = el.dataset.path;
          if (this._slideshowSelected.has(path)) {
            this._slideshowSelected.delete(path);
          } else {
            if (this._slideshowSelected.size >= this._slideshowMaxUploads) return;
            this._slideshowSelected.add(path);
          }
          this._renderOverrideGrid();
        });
      });
    }
  }

  _selectionMatchesBaseline() {
    // After clearing override: empty grid is the correct post-clear state — treat as in-sync.
    if (this._slideshowPostClear) return true;
    const baseline = this._slideshowMode === 'override' ? this._slideshowOverridePaths : this._slideshowCurrentPaths;
    // overridePaths empty while mode=override means the server hasn't confirmed the
    // applied paths yet — treat as in-sync so Apply stays disabled during that window.
    if (this._slideshowMode === 'override' && this._slideshowOverridePaths.length === 0) return true;
    if (this._slideshowSelected.size !== baseline.length) return false;
    return baseline.every(p => this._slideshowSelected.has(p));
  }

  _updateOverrideCounter() {
    // Cancel any pending feedback timers — MQTT response now owns button state
    if (this._applyRestoreTimer) { clearTimeout(this._applyRestoreTimer); this._applyRestoreTimer = null; }
    if (this._clearRestoreTimer) { clearTimeout(this._clearRestoreTimer); this._clearRestoreTimer = null; }
    const count = this._slideshowSelected.size;
    const counterEl = this.querySelector('#ftv-op-counter');
    if (counterEl) counterEl.textContent = `${count} / ${this._slideshowMaxUploads} selected`;
    const applyBtn = this.querySelector('#ftv-op-apply');
    if (applyBtn) {
      applyBtn.textContent = this._slideshowMode === 'override' ? 'Update Override' : 'Apply Override';
      applyBtn.disabled = count === 0 || this._slideshowUploading || this._selectionMatchesBaseline();
    }
    const clearBtn = this.querySelector('#ftv-op-clear');
    if (clearBtn) clearBtn.style.display = this._slideshowMode === 'override' ? '' : 'none';
    const warnEl = this.querySelector('#ftv-op-warn');
    if (warnEl) {
      const { file: _wf } = this._getSelectedData();
      const _wStandby = !_wf || ['standby.png','unknown','unavailable','none',''].includes(String(_wf).trim().toLowerCase());
      const _wLocked = this._slideshowUploading || (_wStandby && this._slideshowMode !== 'override');
      warnEl.style.display = _wLocked ? '' : 'none';
      warnEl.textContent = this._slideshowUploading ? '\u26a0 Uploading \u2014 grid locked' : '\u26a0 Standby \u2014 grid locked';
    }
    const resetBtn = this.querySelector('#ftv-op-reset');
    if (resetBtn) resetBtn.style.display = this._selectionMatchesBaseline() ? 'none' : '';
  }

  _trySetBackground(el, urls) {
      if (!urls || urls.length === 0) return;
      const tryNext = (i) => {
        if (i >= urls.length) return;
        const url = urls[i];
        const img = new Image();
        img.onload = () => {
          el.style.background = `linear-gradient(rgba(0,0,0,0.1), rgba(0,0,0,0.1)), url("${url}")`;
          el.style.backgroundSize = 'cover';
          el.style.backgroundPosition = 'center';
        };
        img.onerror = () => tryNext(i+1);
        img.src = url;
      };
      tryNext(0);
    }
  }

console.info('%c FRAME-TV-ART-CARD %c v0.2.0-beta.2 ', 'color: white; background: #03a9f4; font-weight: bold;', '');

// Register custom element so Lovelace can use <frame-tv-art-card>
try {
  if (!customElements.get('frame-tv-art-card')) {
    customElements.define('frame-tv-art-card', FrameTVArtCard);
  }
} catch (e) {
  console.warn('Failed to register frame-tv-art-card:', e);
}
