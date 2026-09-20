// 卫星跟踪前端逻辑
(function () {
  const $ = id => document.getElementById(id);
  let map = null;
  let currentNorad = null;
  let currentSatName = null; // 当前选中卫星名称
  let trackLayers = {};   // norad -> L.polyline
  let satPosMarkers = {}; // norad -> {circle, label}
  let currentMarker = null;
  let followLast = null;     // 上一帧跟随星下点 (用于 panTo 节流, 防动画堆积卡死)
  let coverageLayer = null; // 卫星覆盖范围圆
  let followSat = false;    // 是否跟随卫星 (点击卫星后开启, 手动拖动地图时关闭)
  let tracking = false;
  let radarPoints = [];
  let radarPass = null;   // 当前显示过境的 pass 元数据 (AOS 标记)
  let selPass = null;     // 当前选择的过境 {aos, los} (点击过境列表选择, 雷达图显示该次完整迹线)
  let obsMarker = null;
  let moonMarker = null, sunMarker = null;   // 月球/太阳位置标记
  let moonVisCircle = null;                   // 月球可见区域圆 (90° 半径)
  let termLine = null;                        // 晨昏线 (太阳星下点 90° 大圆)

  // ===== 地图初始化 (天地图矢量瓦片, Web Mercator 投影) =====
  // ⚠️ 需要使用天地图开发者 token. 免费申请: https://uums.tianditu.gov.cn/register
  // 注册后 → 控制台 → 创建新应用 (选择"浏览器端") → 获取 Key, 填入下方 TDT_KEY
  const TDT_KEY = 'd0ca322ca9f024d7673cd4d91e588290';  // ← 天地图 API Key

  function initMap() {
    map = L.map('map', {
      minZoom: 2, maxZoom: 18,
      zoomControl: false,
      worldCopyJump: true,   // 拖过边缘自动跳转
      preferCanvas: true     // 矢量层用 Canvas 渲染: 缩放时整张画布重绘远快于海量 SVG DOM 节点
    });
    // 缩放按钮放置右下角
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    window.map = map;
// 底图: 天地图矢量瓦片 (含行政边界) — 通过后端代理绕过 WAF
    L.tileLayer('/tdt/vec/{z}/{x}/{y}', {
      maxZoom: 18,
      updateWhenZooming: false,   // 缩放期间沿用旧瓦片, 新瓦片就绪后替换, 减少请求风暴与闪烁
      attribution: '&copy; 天地图'
    }).addTo(map);
    // 预取当前视口瓦片: 打开地图即刻显示, 避免弱链路下逐片等加载
    setTimeout(() => { if (map) map.invalidateSize(); }, 200);
    // 天地图行政标注层 (地名、行政名称)
    if (TDT_KEY) {
      L.tileLayer('/tdt/cva/{z}/{x}/{y}', {
        maxZoom: 18,
        updateWhenZooming: false,   // 缩放沿用旧标注瓦片, 减少请求与重绘
        attribution: '&copy; 天地图'
      }).addTo(map);
    }
    // OSM 兜底: 如果天地图 vec_w 因网络问题未加载, 不额外处理
    // 以观测站为中心并显示观测站位置
    fetch('/api/sat/observer').then(r => r.json()).then(d => {
      if (d.ok) {
        map.setView([d.lat, d.lon], 3);
        window.obsPos = [d.lat, d.lon];
        obsMarker = L.circleMarker([d.lat, d.lon], {
          radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1
        }).addTo(map).bindPopup('观测站');
      }
    }).catch(() => { map.setView([20, 0], 2); window.obsPos = [20, 0]; });
    // ponytail: 暴露观测站标记给 inline script 画方向线
    window.obsMarker = obsMarker;

    // 天体图层: 月球/太阳位置 + 月球可见区域 + 晨昏线 (位置由 SSE cel 帧更新)
    moonMarker = L.marker([0, 0], {
      icon: L.divIcon({ className: 'cel-marker', html: '<span style="font-size:18px;text-shadow:0 0 6px rgba(167,139,250,.9)">🌙</span>', iconSize: [22, 22], iconAnchor: [11, 11] }),
      interactive: false, keyboard: false
    }).addTo(map);
    sunMarker = L.marker([0, 0], {
      icon: L.divIcon({ className: 'cel-marker', html: '<span style="font-size:20px;text-shadow:0 0 6px rgba(251,191,36,.9)">☀️</span>', iconSize: [24, 24], iconAnchor: [12, 12] }),
      interactive: false, keyboard: false
    }).addTo(map);
    moonVisCircle = L.circle([0, 0], {
      radius: 90 * 111195,   // 90° 大圆 ≈ 10018 km
      color: '#a78bfa', weight: 1, dashArray: '4 5',
      fillColor: '#a78bfa', fillOpacity: 0.06, interactive: false
    }).addTo(map);
    termLine = L.polyline([], {
      color: '#fbbf24', weight: 1.5, opacity: 0.85, dashArray: '6 4',
      interactive: false, noClip: true   // 允许跨过反经线整圈绘制
    }).addTo(map);
    // 有数据前隐藏
    moonMarker.setOpacity(0); sunMarker.setOpacity(0);
    moonVisCircle.setStyle({ opacity: 0, fillOpacity: 0 });
    termLine.setStyle({ opacity: 0 });
  }

  // 保存观测站后刷新地图中心与标记
  async function updateObserverOnMap() {
    try {
      const r = await fetch('/api/sat/observer');
      const d = await r.json();
      if (d.ok) {
        map.setView([d.lat, d.lon], 3);
        window.obsPos = [d.lat, d.lon];
        if (obsMarker) map.removeLayer(obsMarker);
        obsMarker = L.circleMarker([d.lat, d.lon], {
          radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1
        }).addTo(map).bindPopup('观测站');
        window.obsMarker = obsMarker;
      }
    } catch (e) { /* 忽略 */ }
  }
  window.updateObserverOnMap = updateObserverOnMap;

  // ===== 梅登海德网格 (场级线) + 4 字符文字 + VUCC 已确认网格叠加层 =====
  let gridLineLayer = null;    // 场级网格线 (20°×10°, 2 字符 AA-RR)
  let gridLabelLayer = null;   // 4 字符网格文字标注
  let vuccLayer = null;        // VUCC 已确认网格方块
  const MIN_GRID_ZOOM = 5;     // 低于该缩放级别不显示网格 (低级别下网格过密)
  const VUCC_COLORS = { '6m': '#22c55e', '2m': '#3b82f6', '70cm': '#f59e0b', 'sat': '#a78bfa' };
  const VUCC_ORDER = ['6m', '2m', '70cm', 'sat'];

  // 梅登海德 4 字符网格 (2°×1°) 顶点坐标
  function gridCellCorners(grid) {
    const g = grid.toUpperCase();
    const lon = -180 + (g.charCodeAt(0) - 65) * 20 + Number(g[2]) * 2;
    const lat = -90 + (g.charCodeAt(1) - 65) * 10 + Number(g[3]);
    return [[lat, lon], [lat, lon + 2], [lat + 1, lon + 2], [lat + 1, lon]];
  }

  // 网格线: 场级 (20°×10°) 浅黄 + 4 字符级 (2°×1°) 浅灰细线
  function buildGridLines() {
    if (gridLineLayer) { map.removeLayer(gridLineLayer); gridLineLayer = null; }
    if (map.getZoom() < MIN_GRID_ZOOM) return;
    const lines = [];
    for (let lon = -180; lon <= 180; lon += 20) {
      lines.push([[90, lon], [-90, lon]]);
    }
    for (let lat = -90; lat <= 90; lat += 10) {
      lines.push([[lat, -180], [lat, 180]]);
    }
    gridLineLayer = L.layerGroup(
      lines.map(pts => L.polyline(pts, { color: '#fbbf24', weight: 0.8, opacity: 0.3, interactive: false }))
    ).addTo(map);
    // 4 字符级网格线 (2°×1°), 浅灰细线
    const fine = [];
    for (let lon = -180; lon <= 180; lon += 2) {
      fine.push([[90, lon], [-90, lon]]);
    }
    for (let lat = -90; lat <= 90; lat += 1) {
      fine.push([[lat, -180], [lat, 180]]);
    }
    gridLineLayer.addLayer(L.layerGroup(
      fine.map(pts => L.polyline(pts, { color: '#9ca3af', weight: 0.5, opacity: 0.3, interactive: false }))
    ));
  }

  // 4 字符网格文字标注 (字号随缩放级别变化, 保持相对网格的固定比例)
  function buildGridLabels() {
    if (gridLabelLayer) { map.removeLayer(gridLabelLayer); gridLabelLayer = null; }
    if (map.getZoom() < MIN_GRID_ZOOM) return;
    const z = map.getZoom();
    // 2° 经度在当前 zoom 下的像素宽 (Web Mercator: 256*2^z px = 360°)
    const gridW = 256 * Math.pow(2, z) * (2 / 360);
    // 4 字符等宽文字宽 ≈ 2.4×字号, 让文字宽占网格宽的 ~30%
    const fs = Math.max(12, Math.round(gridW * 0.125));
    const b = map.getBounds();
    const lon0 = Math.max(-180, Math.floor(b.getWest() / 2) * 2);
    const lon1 = Math.min(180, Math.ceil(b.getEast() / 2) * 2);
    const lat0 = Math.max(-90, Math.floor(b.getSouth()));
    const lat1 = Math.min(90, Math.ceil(b.getNorth()));
    const labels = [];
    for (let lon = lon0; lon < lon1; lon += 2) {
      for (let lat = lat0; lat < lat1; lat += 1) {
        const ch1 = String.fromCharCode(65 + Math.floor((lon + 180) / 20));
        const ch2 = String.fromCharCode(65 + Math.floor((lat + 90) / 10));
        const d1 = Math.floor(((lon + 180) % 20) / 2);
        const d2 = Math.floor((lat + 90) % 10);
        const grid = `${ch1}${ch2}${d1}${d2}`;
        labels.push(L.marker([lat + 0.5, lon + 1], {
          icon: L.divIcon({
            className: 'grid-label',
            html: `<span style="font-size:${fs}px;line-height:${Math.round(fs * 1.2)}px;">${grid}</span>`,
            iconSize: [Math.round(fs * 2.6), Math.round(fs * 1.3)],
            iconAnchor: [Math.round(fs * 1.3), Math.round(fs * 0.65)]
          }),
          interactive: false, keyboard: false
        }));
      }
    }
    if (labels.length) gridLabelLayer = L.layerGroup(labels).addTo(map);
  }

  // 视口变化时按缩放级别显示/隐藏网格 (避免海量 marker 常驻拖垮地图)
  function bindGridLabelRefresh() {
    map.on('moveend zoomend', () => {
      if (!window.__gridEnabled) return;
      if (map.getZoom() < MIN_GRID_ZOOM) {
        if (gridLineLayer) { map.removeLayer(gridLineLayer); gridLineLayer = null; }
        if (gridLabelLayer) { map.removeLayer(gridLabelLayer); gridLabelLayer = null; }
        return;
      }
      buildGridLines();
      buildGridLabels();
    });
  }

  // VUCC 已确认网格方块 (按设置中勾选的频段过滤)
  function buildVuccLayer() {
    if (vuccLayer) { map.removeLayer(vuccLayer); vuccLayer = null; }
    const checked = new Set(Array.from(document.querySelectorAll('.vucc-band:checked')).map(cb => cb.value));
    if (!checked.size) return;
    fetch('/api/lotw').then(r => r.json()).then(d => {
      if (!d.ok || !d.bands) return;
      const rects = [];
      for (const band of VUCC_ORDER) {
        if (!checked.has(band)) continue;
        const color = VUCC_COLORS[band] || '#22c55e';
        (d.bands[band] || []).forEach(g => {
          rects.push(L.polygon(gridCellCorners(g), {
            color: color, weight: 1, fillColor: color, fillOpacity: 0.45, interactive: false
          }));
        });
      }
      if (rects.length) vuccLayer = L.layerGroup(rects).addTo(map);
    }).catch(() => { /* 忽略 */ });
  }

  // 设置变化后刷新叠加层 (由设置面板保存后调用)
  async function applyGridSettings() {
    try {
      const r = await fetch('/api/settings');
      const d = await r.json();
      const show = d.ok && !!d.show_maidenhead_grid;
      window.__gridEnabled = show;
      if (show) { buildGridLines(); buildGridLabels(); }
      else {
        if (gridLineLayer) { map.removeLayer(gridLineLayer); gridLineLayer = null; }
        if (gridLabelLayer) { map.removeLayer(gridLabelLayer); gridLabelLayer = null; }
      }
    } catch (e) { /* 忽略 */ }
    buildVuccLayer();
  }
  window.applyGridSettings = applyGridSettings;

  // ===== 倒计时格式化 =====
  function formatCountdown(sec) {
    sec = Math.floor(sec);
    if (sec <= 0) return '正在过境';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}小时${m}分`;
    if (m > 0) return `${m}分${s}秒`;
    return `${s}秒`;
  }

  // ===== 收藏列表 (按下次过境时间排序) =====
  function renderFavorites(data) {
    const list = $('favList');
      list.innerHTML = '';
      const now = Date.now() / 1000;
      const cur = String(currentNorad ?? '');
      data.favorites.forEach(f => {
        const item = document.createElement('div');
        item.className = 'fav-item' + (String(f.norad) === cur ? ' active' : '');
        item.dataset.aos = f.next_aos || '';
        item.dataset.status = f.status || '';
        let infoHtml = '';
        if (f.status === 'in_pass') {
          infoHtml = `<span class="countdown in-pass">正在过境</span> · 最大仰角 ${f.max_el}°`;
        } else if (f.status === 'upcoming' && f.next_aos) {
          const sec = Math.max(0, f.next_aos - now);
          const urgentClass = sec < 1800 ? ' urgent' : '';
          infoHtml = `<span class="countdown${urgentClass}">${formatCountdown(sec)}后过境</span> · 最大仰角 ${f.max_el}°`;
        } else if (f.status === 'pending') {
          infoHtml = `<span class="countdown">正在获取过境数据...</span>`;
        } else {
          infoHtml = `<span class="countdown">未来48小时无过境</span>`;
        }
        item.innerHTML = `<div><div class="name">${f.name || f.norad}</div><div class="norad">NORAD ${f.norad}</div><div class="pass-info">${infoHtml}</div></div>
          <button class="del" data-norad="${f.norad}">删除</button>`;
        item.addEventListener('click', (e) => {
          if (e.target.classList.contains('del')) return;
          if (String(f.norad) === String(currentNorad)) {
            // 再次点击已选中卫星: 取消选中并跳回观测站
            clearSatellite();
            const obs = window.obsPos || [20, 0];
            map.setView(obs, 3);
            return;
          }
          selectSatellite(f.norad, f.name || f.norad);
        });
        item.querySelector('.del').addEventListener('click', async (e) => {
          e.stopPropagation();
          const res = await fetch('/api/sat/favorites/' + f.norad, { method: 'DELETE' });
          const d = await res.json().catch(() => ({}));
          if (!d.ok) { toast('删除失败: ' + (d.error || d.detail || '')); return; }
          delete trackLayers[f.norad];
          if (satPosMarkers[f.norad]) {
            map.removeLayer(satPosMarkers[f.norad].circle);
            map.removeLayer(satPosMarkers[f.norad].label);
            delete satPosMarkers[f.norad];
          }
          if (f.norad === currentNorad) { currentNorad = null; clearSatellite(); }
          loadFavorites();
        });
        list.appendChild(item);
      });
      // 未选中卫星: 显示实时位置 (圆圈+名称); 选中卫星: 显示轨迹
      data.favorites.forEach(f => {
        if (String(f.norad) === cur) loadTrack(f.norad, trackHours);
        else loadSatPos(f.norad, f.name || f.norad);
      });
      restoreLastSelection(data.favorites);
  }

  async function loadFavorites() {
    try {
      const res = await fetch('/api/sat/favorites');
      const data = await res.json();
      if (!data.ok) return;
      renderFavorites(data);
    } catch (e) { /* 忽略 */ }
  }

  // ===== 恢复上次选中卫星 (优先: 跟踪中的 → localStorage 的) =====
  let restoredOnce = false;  // 只自动恢复一次, 之后尊重用户手动取消选中的状态
  function restoreLastSelection(favorites) {
    if (currentNorad || restoredOnce) return;
    // 1) 刷新时后端仍在跟踪的卫星
    if (pendingRestoreNorad != null) {
      const hit = favorites.find(f => String(f.norad) === String(pendingRestoreNorad));
      if (hit) {
        restoredOnce = true;
        const n = pendingRestoreNorad;
        pendingRestoreNorad = null;
        selectSatellite(Number(n), hit.name || n, { follow: false });
        return;
      }
    }
    pendingRestoreNorad = null;
    // 2) 上次页面选中的卫星
    try {
      const last = JSON.parse(localStorage.getItem('lastSat') || 'null');
      if (last && last.norad != null) {
        const hit = favorites.find(f => String(f.norad) === String(last.norad));
        if (hit) {
          restoredOnce = true;
          selectSatellite(hit.norad, hit.name || last.norad, { follow: false });
        }
      }
    } catch (e) { /* 忽略 */ }
  }

  // 每秒更新收藏列表倒计时
  setInterval(() => {
    const now = Date.now() / 1000;
    document.querySelectorAll('.fav-item').forEach(item => {
      const el = item.querySelector('.countdown');
      if (!el) return;
      const status = item.dataset.status;
      const aos = parseFloat(item.dataset.aos);
      if (status === 'in_pass') {
        el.textContent = '正在过境';
        el.className = 'countdown in-pass';
      } else if (status === 'upcoming' && aos) {
        const sec = Math.max(0, aos - now);
        el.textContent = formatCountdown(sec) + '后过境';
        el.className = 'countdown' + (sec < 1800 ? ' urgent' : '');
      }
    });
  }, 1000);

  // ===== 未选中卫星实时位置 (圆圈 + 名称) =====
  async function loadSatPos(norad, name) {
    // 当前选中卫星由 currentMarker (脉冲点+标签) 负责显示, 避免重复标记叠在同一位置
    if (currentNorad != null && String(norad) === String(currentNorad)) return;
    // 节流: 已有标记且 60s 内更新过则跳过, 避免 favorites 帧触发 N 个位置请求堆积
    const ex = satPosMarkers[norad];
    if (ex && Date.now() - ex.ts < 60000) return;
    try {
      const res = await fetch('/api/sat/position/' + norad);
      const data = await res.json();
      if (!data.ok) return;
      if (satPosMarkers[norad]) {
        map.removeLayer(satPosMarkers[norad].circle);
        map.removeLayer(satPosMarkers[norad].label);
      }
      const circle = L.circleMarker([data.sub_lat, data.sub_lon], {
        radius: 5, color: '#facc15', fillColor: '#facc15', fillOpacity: 0.8,
        className: 'sat-dot-clickable'
      }).addTo(map);
      const label = L.marker([data.sub_lat, data.sub_lon], {
        icon: L.divIcon({ className: 'sat-label clickable', html: name, iconSize: [90, 16] })
      }).addTo(map);
      // 点击地图上的卫星圆点/名称 → 直接切换选中
      const pick = () => selectSatellite(norad, name);
      circle.on('click', pick);
      label.on('click', pick);
      satPosMarkers[norad] = { circle, label, ts: Date.now() };
    } catch (e) { /* 忽略 */ }
  }

  // ===== 卫星实时搜索下拉 (名称/编号模糊匹配, 点击即收藏) =====
  const favSearchRes = $('favSearchRes');
  let searchTimer = null;

  function hideSearchRes() { favSearchRes.style.display = 'none'; }

  async function doLiveSearch(q) {
    try {
      const res = await fetch('/api/sat/search?q=' + encodeURIComponent(q));
      const data = await res.json();
      if (!data.ok) { hideSearchRes(); return; }
      const matches = data.satellites || [];
      if (matches.length === 0) {
        favSearchRes.innerHTML = '<div class="res-empty">未找到匹配卫星</div>';
        favSearchRes.style.display = 'block';
        return;
      }
      favSearchRes.innerHTML = matches.map(s =>
        `<div class="res-item" data-norad="${s.norad}"><span class="r-name">${s.name || s.norad}</span><span class="r-norad">NORAD ${s.norad}</span></div>`
      ).join('');
      favSearchRes.style.display = 'block';
      favSearchRes.querySelectorAll('.res-item').forEach(el => {
        el.addEventListener('click', () => {
          addFav(el.dataset.norad);
          $('favSearch').value = '';
          hideSearchRes();
        });
      });
    } catch (e) { hideSearchRes(); }
  }

  $('favSearch').addEventListener('input', () => {
    const q = $('favSearch').value.trim();
    clearTimeout(searchTimer);
    if (!q) { hideSearchRes(); return; }
    searchTimer = setTimeout(() => doLiveSearch(q), 250);
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.fav-search-wrap')) hideSearchRes();
  });

  // ===== 添加收藏 =====
  $('btnFavAdd').onclick = async () => {
    const q = $('favSearch').value.trim();
    if (!q) { toast('请输入搜索词'); return; }
    try {
      // 后端检索: 名称/编号包含匹配, 最多返回 20 条, 无需拉全量 TLE
      const res = await fetch('/api/sat/search?q=' + encodeURIComponent(q));
      const data = await res.json();
      if (!data.ok) return;
      const matches = data.satellites;
      if (matches.length === 0) { toast('未找到卫星'); return; }
      if (matches.length === 1) {
        await addFav(matches[0].norad);
      } else {
        // 多个匹配, 用第一个
        await addFav(matches[0].norad);
        toast(`找到 ${matches.length} 个, 已添加第一个`);
      }
    } catch (e) { toast('搜索失败'); }
  };
  async function addFav(norad) {
    try {
      const res = await fetch('/api/sat/favorites', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ norad }) });
      const data = await res.json();
      if (data.ok) { toast('已添加收藏'); loadFavorites(); }
      else toast('添加失败: ' + (data.detail || ''));
    } catch (e) { toast('添加失败'); }
  }

  // ===== 经度展开 (跨日界线连续化, 避免横穿地图的长线) =====
  function unwrapLng(points) {
    if (points.length === 0) return [];
    const result = [[points[0][0], points[0][1]]];
    let offset = 0;
    for (let i = 1; i < points.length; i++) {
      let dlon = points[i][1] - points[i - 1][1];
      if (dlon > 180) offset -= 360;       // 179 -> -179, 补偿 -360
      else if (dlon < -180) offset += 360;  // -179 -> 179, 补偿 +360
      result.push([points[i][0], points[i][1] + offset]);
    }
    return result;
  }

  // ===== 加载卫星轨迹 (星下点) =====
  async function loadTrack(norad, hours, seq) {
    try {
      const res = await fetch('/api/sat/track/' + norad + '?hours=' + (hours || 24) + '&step=120');
      const data = await res.json();
      if (seq !== undefined && seq !== selectSeq) return;  // 已切换到其他卫星, 丢弃旧响应
      if (!data.ok || !data.points || data.points.length === 0) return;
      const raw = data.points.map(p => [p.lat, p.lon]);
      if (trackLayers[norad]) {
        map.removeLayer(trackLayers[norad]);
        trackLayers[norad] = null;
      }
      // 展开经度使轨迹连续, 然后渲染 3 份 (-360/0/+360) 保证左右滚动都有轨迹
      const unwrapped = unwrapLng(raw);
      const layers = [];
      for (let off = -360; off <= 360; off += 360) {
        const pts = unwrapped.map(p => [p[0], p[1] + off]);
        layers.push(L.polyline(pts, { color: '#0ea5e9', weight: 2, opacity: 0.8 }).addTo(map));
      }
      trackLayers[norad] = L.layerGroup(layers).addTo(map);
    } catch (e) { /* 忽略 */ }
  }

  // ===== 轨迹时长切换 (1h/3h/6h/12h) =====
  let trackHours = 12;  // 地图轨迹默认时长
  function bindTrackDurBtns() {
    document.querySelectorAll('#durBtns .dur-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        trackHours = parseInt(btn.dataset.hours, 10);
        document.querySelectorAll('#durBtns .dur-btn').forEach(b => b.classList.toggle('active', b === btn));
        if (currentNorad) loadTrack(currentNorad, trackHours);
      });
    });
  }

  // ===== 选择卫星 =====
  function clearAllTracks() {
    Object.keys(trackLayers).forEach(k => { if (trackLayers[k]) map.removeLayer(trackLayers[k]); });
    trackLayers = {};
  }
  let selectSeq = 0;  // 切换令牌: 快速连续切换时旧请求响应直接丢弃, 防止旧数据覆盖新状态
  async function selectSatellite(norad, name, opts = {}) {
    const seq = ++selectSeq;
    // 切换卫星时移除旧轨迹
    clearAllTracks();
    // 立即清空雷达图, 防止旧卫星迹线残留/慢加载期间显示错卫星
    radarPoints = [];
    radarPass = null;
    drawRadar([], null, null);
    currentNorad = norad;
    currentSatName = name || norad;
    // 切换卫星: 重置星下点 marker (重建以刷新名称标签) 与跟随节流记录
    if (currentMarker) { map.removeLayer(currentMarker); currentMarker = null; }
    followLast = null;
    try { localStorage.setItem('lastSat', JSON.stringify({ norad, name: currentSatName })); } catch (e) { /* 忽略 */ }
    // 高亮列表
    document.querySelectorAll('.fav-item').forEach(el => el.classList.remove('active'));
    const items = document.querySelectorAll('.fav-item');
    items.forEach(el => {
      // 太阳/月球等天体项无 .del 按钮, 需判空
      const delBtn = el.querySelector('.del');
      if (delBtn && String(delBtn.dataset.norad) === String(norad)) el.classList.add('active');
    });
    // 移除该卫星的实时位置圆圈, 显示 24 小时轨迹
    if (satPosMarkers[norad]) {
      map.removeLayer(satPosMarkers[norad].circle);
      map.removeLayer(satPosMarkers[norad].label);
      delete satPosMarkers[norad];
    }
    loadTrack(norad, trackHours, seq);
    selPass = null;                 // 切换卫星: 重置所选过境, 雷达图自动显示当前/下一次过境
    loadRadar(norad, seq, null);
    loadPasses(norad, seq);
    setStreamTarget({ norad });
    // 移动端: 点击卫星后地图右上角显示浮动雷达图
    const rf = $('radarFloat');
    if (rf) rf.classList.add('show');
    // 点击卫星: 开启跟随, 地图中心移至卫星 (opts.follow=false 用于刷新恢复, 保持地图停在观测站)
    followSat = (opts.follow !== false);
    if (followSat) map.once('dragstart', () => { followSat = false; });  // 手动拖动时取消跟随
  }

  function clearSatellite() {
    currentNorad = null;
    currentSatName = null;
    followSat = false;
    try { localStorage.removeItem('lastSat'); } catch (e) { /* 忽略 */ }
    $('satInfo').innerHTML = '<div class="row"><span class="k">未选择卫星</span></div>';
    if (currentMarker) { map.removeLayer(currentMarker); currentMarker = null; }
    if (coverageLayer) { map.removeLayer(coverageLayer); coverageLayer = null; }
    setStreamTarget(null);
    $('passList').innerHTML = '';
    radarPoints = [];
    radarPass = null;
    selPass = null;
    drawRadar([], null, null);
    // 移动端: 清除选择后隐藏浮动雷达图
    const rf = $('radarFloat');
    if (rf) rf.classList.remove('show');
    // 移除所有轨迹, 恢复实时位置圆圈
    Object.keys(trackLayers).forEach(k => { if (trackLayers[k]) map.removeLayer(trackLayers[k]); });
    trackLayers = {};
    loadFavorites();
  }

  // 卫星覆盖圆半径: 仰角>=0 的地面范围 (米)
  function coverageRadius(altKm) {
    const R = 6371;
    let h = Number(altKm);
    // 异常高度 (undefined/NaN/过低) 用典型 LEO 高度兜底, 避免画出异常小圆
    if (!isFinite(h) || h < 300) h = 550;
    const gamma = Math.acos(R / (R + h)); // 地心角 rad
    return gamma * R * 1000;
  }
  // 绘制/更新覆盖圆 (圆心跟随星下点, 半径每帧同步当前卫星高度:
  // 否则创建时用旧卫星(如低轨)算的半径, 切换卫星后只动圆心半径不变 → 圈偏小/偏大)
  function drawCoverage(subLat, subLon, altKm) {
    if (subLat == null || subLon == null || altKm == null) return;
    const latlng = [subLat, subLon];
    const radius = coverageRadius(altKm);
    if (coverageLayer) {
      // 值未变时跳过 setLatLng/setRadius, 避免每帧触发地图 repaint 堆积卡死
      const cur = coverageLayer.getLatLng();
      if (Math.abs(cur.lat - subLat) > 0.05 || Math.abs(cur.lng - subLon) > 0.05) coverageLayer.setLatLng(latlng);
      if (Math.abs(coverageLayer.getRadius() - radius) > 1) coverageLayer.setRadius(radius);
      return;
    }
    coverageLayer = L.circle(latlng, {
      radius: radius,
      color: '#0ea5e9', weight: 1,
      fillColor: '#0ea5e9', fillOpacity: 0.08
    }).addTo(map);
  }

  // ===== 卫星信息渲染 (数据由 SSE state 帧提供; enc 提供云台指向角度) =====
  function renderSatState(data, enc) {
    if (!data) return;
    $('satInfo').innerHTML = `
      <div class="row"><span class="k">方位角</span><span class="v">${data.azimuth}°</span></div>
      <div class="row"><span class="k">仰角</span><span class="v">${data.elevation}°</span></div>
      <div class="row"><span class="k">距离</span><span class="v">${data.distance} km</span></div>
      <div class="row"><span class="k">可见</span><span class="v" style="color:${data.visible ? '#4ade80' : '#ef4444'}">${data.visible ? '是' : '否'}</span></div>`;
    // 覆盖范围圆 (跟随星下点)
    drawCoverage(data.sub_lat, data.sub_lon, data.alt_km);
    // 地图上当前星下点标记: 复用 marker 仅 setLatLng, 禁止每帧 removeLayer/重建 (会造成 GC+重排卡顿)
    const latlng = [data.sub_lat, data.sub_lon];
    const labelText = currentSatName || '卫星';
    if (!currentMarker) {
      currentMarker = L.layerGroup([
        L.marker(latlng, {
          icon: L.divIcon({
            className: 'cur-sat-wrap',
            html: '<span class="cur-sat-pulse"></span><span class="cur-sat-dot"></span>',
            iconSize: [18, 18], iconAnchor: [9, 9]
          })
        }),
        L.marker(latlng, {
          icon: L.divIcon({ className: 'sat-label', html: labelText, iconSize: [100, 16] })
        })
      ]).addTo(map);
    } else {
      currentMarker.getLayers().forEach(mk => mk.setLatLng(latlng));
    }
    // 跟随卫星: 星下点位移较大才触发平移动画, 避免每帧 panTo(animate) 打断重启动画导致卡死
    if (followSat) {
      const d = followLast && (Math.abs(data.sub_lat - followLast[0]) +
                               Math.abs(data.sub_lon - followLast[1]) > 1.2);
      followLast = [data.sub_lat, data.sub_lon];
      if (!d) map.panTo(latlng, { animate: true, duration: 0.4 });
    }
    // 雷达图当前点 + 云台指向 (与地图方向线一致: 优先 dpan, 无则 pan)
    const panVal = enc && (enc.dpan != null ? Number(enc.dpan) : enc.pan);
    drawRadar(radarPoints, { az: data.azimuth, el: data.elevation, pan: panVal }, radarPass);
  }

  // ===== 过境列表 =====
  async function loadPasses(norad, seq) {
    try {
      // 加载占位提示
      $('passList').innerHTML = '<div style="font-size:12px;color:#64748b;padding:4px 2px">加载过境中…</div>';
      const res = await fetch('/api/sat/passes/' + norad + '?hours=24&min_elev=0');
      const data = await res.json();
      if (seq !== undefined && seq !== selectSeq) return;  // 已切换, 丢弃旧响应
      if (!data.ok) return;
      const list = $('passList');
      list.innerHTML = '<div style="font-size:13px;color:#7dd3fc;margin-bottom:8px">未来过境</div>';
      data.passes.slice(0, 5).forEach(p => {
        const aos = new Date(p.aos * 1000);
        const los = new Date(p.los * 1000);
        const fmt = d => d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        const sel = selPass && Math.abs(selPass.aos - p.aos) < 5 ? ' selected' : '';
        list.innerHTML += `<div class="pass-item${sel}" data-aos="${p.aos}" data-los="${p.los}">
          <div>${fmt(aos)} - ${fmt(los)}</div>
          <div class="t">最大仰角 ${p.max_el}° · 方位 ${p.aos_az}°→${p.los_az}° · ${Math.round(p.duration / 60)}分</div></div>`;
      });
      // 点击过境项: 雷达图切换为该次过境的完整 AOS→LOS 迹线
      list.onclick = e => {
        const it = e.target.closest('.pass-item');
        if (!it || !currentNorad) return;
        selectPass(currentNorad, { aos: parseFloat(it.dataset.aos), los: parseFloat(it.dataset.los) });
      };
    } catch (e) { /* 忽略 */ }
  }

  // ===== 选择过境: 雷达图显示该次过境 AOS→LOS 完整迹线 =====
  function selectPass(norad, pass) {
    selPass = pass;
    loadRadar(norad, selectSeq, pass);
    document.querySelectorAll('.pass-item').forEach(el => {
      el.classList.toggle('selected', Math.abs(parseFloat(el.dataset.aos) - pass.aos) < 5);
    });
  }

  // ===== 雷达图 =====
  async function loadRadar(norad, seq, pass) {
    const q = pass ? '?aos=' + pass.aos + '&los=' + pass.los : '';
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await fetch('/api/sat/radar/' + norad + q);
        const data = await res.json();
        if (seq !== undefined && seq !== selectSeq) return;  // 已切换, 丢弃旧响应
        if (!data.ok) { if (attempt < 2) { await new Promise(r => setTimeout(r, 600 * (attempt + 1))); continue; } return; }
        radarPoints = data.points || [];
        radarPass = data.pass || null;
        drawRadar(radarPoints, null, radarPass);
        return;
      } catch (e) {
        if (attempt < 2) { await new Promise(r => setTimeout(r, 600 * (attempt + 1))); continue; }
        // 3 次失败: 雷达图保留空白 (SSE 帧仍会带实时点重绘)
      }
    }
  }

  function drawRadar(points, current, passInfo) {
    // 双画布: 桌面右侧面板 + 移动端地图右上角浮动雷达, 绘制逻辑一致
    [$('radar'), $('radarFloatCv')].forEach(canvas => {
      if (!canvas) return;
      _paintRadar(canvas, points, current, passInfo);
    });
  }

  function _paintRadar(canvas, points, current, passInfo) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 12;
    ctx.clearRect(0, 0, W, H);
    // 网格
    ctx.strokeStyle = '#334155';
    ctx.lineWidth = 1;
    [1 / 3, 2 / 3, 1].forEach(f => {
      ctx.beginPath();
      ctx.arc(cx, cy, R * f, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();
    // 方位标注
    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('N', cx, cy - R - 4);
    ctx.fillText('E', cx + R + 10, cy + 4);
    ctx.fillText('S', cx, cy + R + 14);
    ctx.fillText('W', cx - R - 10, cy + 4);
    // 轨迹: 始终绘制完整预测路径, 地平线(el<0)以下用暗色段连接, 不再断裂成残缺片段
    if (points && points.length > 1) {
      const xy = p => {
        // el 为负时仍连续绘制 (r 钳制在外圈内), 保证过境弧线完整
        const r = R * Math.max(0, Math.min(1.1, 1 - p.el / 90));
        return [cx + r * Math.sin(p.az * Math.PI / 180),
                cy - r * Math.cos(p.az * Math.PI / 180)];
      };
      // 地平线下段: 暗色半透明 (未升起部分)
      ctx.strokeStyle = 'rgba(148,163,184,0.45)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      let g1 = false;
      points.forEach(p => {
        if (p.el < 0) { const [x, y] = xy(p); g1 ? ctx.lineTo(x, y) : ctx.moveTo(x, y); g1 = true; }
        else g1 = false;
      });
      ctx.stroke();
      // 地平线上段: 亮黄实线
      ctx.setLineDash([]);
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2;
      ctx.beginPath();
      let started = false;
      points.forEach(p => {
        const [x, y] = xy(p);
        if (p.el < 0) { started = false; return; }
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    // 当前卫星位置
    if (current) {
      const r = R * (1 - current.el / 90);
      const x = cx + r * Math.sin(current.az * Math.PI / 180);
      const y = cy - r * Math.cos(current.az * Math.PI / 180);
      ctx.fillStyle = '#0ea5e9';
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
      // 云台指向指示: 从圆心指向当前 pan 方位 (绿色虚线 + 端点圆)
      // 与卫星方位同坐标系 (0°=北, 顺时针); pan 无效时跳过
      if (current.pan != null && !isNaN(current.pan)) {
        const pa = current.pan * Math.PI / 180;
        const px = cx + (R - 8) * Math.sin(pa);
        const py = cy - (R - 8) * Math.cos(pa);
        ctx.strokeStyle = '#22c55e';
        ctx.lineWidth = 2.5;
        ctx.setLineDash([7, 4]);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(px, py);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#22c55e';
        ctx.beginPath();
        ctx.arc(px, py, 4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    // 过境 AOS 起点标记 (外圈, el≈0)
    if (passInfo && passInfo.aos_az !== undefined) {
      const x = cx + R * Math.sin(passInfo.aos_az * Math.PI / 180);
      const y = cy - R * Math.cos(passInfo.aos_az * Math.PI / 180);
      ctx.fillStyle = '#22c55e';
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('AOS', x, y - 10);
    }
  }

  // ===== AS5600 实时标定面板 UI =====
  function formatPan(v) {
    if (v === null || v === undefined) return '-';
    // pan 0~360°: 360° 与 0° 同位置 (光电零位), 显示为 0°
    let val = v;
    if (val >= 359.95) val = 0.0;
    return val.toFixed(2);
  }

  function updateEncCalUi(d) {
    const latest = d || {};
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('encCalRaw', latest.raw ?? '-');
    // angle: ESP32 回传原始累积角 (与控制组件下方 ang 一致), cont_angle 是除以 4 的物理连续角
    set('encCalCont', latest.angle ?? '-');
    set('encCalPan', formatPan(latest.pan));
    set('encCalRpd', latest.zero_angle !== undefined && latest.zero_angle !== null
      ? Number(latest.zero_angle).toFixed(2) + '°' : '-');
  }

  async function loadEncCal() {
    try {
      const r = await fetch('/api/encoder');
      const d = await r.json();
      if (d.ok) updateEncCalUi(d);
    } catch (e) { /* 忽略 */ }
  }

  $('encCalToggle').onclick = () => {
    $('encCalBody').classList.toggle('hidden');
    $('encCalToggle').classList.toggle('collapsed');
  };

  // ===== 光电自动校零 + 测速 (状态由 SSE photocalib 帧提供) =====
  function renderPhotoCalib(d) {
    if (!d) return;
    const st = $('encPhotoStatus');
    if (!st) return;
    const modeName = '自动校零';
    if (d.running) {
      const phase = d.phase === 'waiting_first' ? '等待光电触发起点…' :
                    d.phase === 'waiting_second' ? '已触发起点，转一圈中，等待终点…' : '处理中…';
      st.innerHTML = `⏳ 光电${modeName}中：${phase}`;
    } else if (d.result && d.result.ok) {
      const revs = d.result.as5600_revs;
      const ratioTxt = (revs !== null && revs !== undefined) ? `${revs.toFixed(2)}:1` : '-';
      st.innerHTML = `✅ 光电${modeName}完成：传动比 <b>${ratioTxt}</b>（AS5600 转角 <b>${d.result.angle_delta?.toFixed(1) ?? '-'}°</b>，0 基准角 <b>${d.result.zero_angle?.toFixed(1) ?? '-'}°</b>）` +
        (d.result.pan_speed_dps ? `，水平速度已校准 <b>${d.result.pan_speed_dps.toFixed(3)}</b>°/s` : '') +
        `（一圈耗时 ${d.result.elapsed}s）`;
      loadEncCal();
    } else if (d.result && !d.result.ok) {
      st.innerHTML = `❌ ${d.result.detail || '光电' + modeName + '失败'}`;
    } else {
      st.innerHTML = '';
    }
  }

  async function startPhotoCalib() {
    if (!confirm('将让云台自动转一圈，利用光电传感器自动完成校零与测速，确定？')) return;
    try {
      const r = await fetch('/api/encoder/photocalib', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'start' })
      });
      const d = await r.json();
      if (d.ok) { toast(d.msg || '已开始'); }
      else toast(d.detail || '启动失败');
    } catch (e) { toast('启动失败: ' + e.message); }
  }

  async function manualSetZero() {
    if (!confirm('将当前回传角度设为物理 0° 基准角 (zero_angle)，确定？')) return;
    try {
      const r = await fetch('/api/encoder/setzero', { method: 'POST' });
      const d = await r.json();
      if (d.ok) { toast(d.msg || '已设置'); loadEncCal(); }
      else toast(d.detail || '设置失败');
    } catch (e) { toast('设置失败: ' + e.message); }
  }

  $('btnEncPhoto').onclick = () => startPhotoCalib();
  $('btnEncSetZero').onclick = () => manualSetZero();

  // ===== 云台跟踪 =====
  $('btnTrack').onclick = async () => {
    if (tracking) {
      // 正在跟踪 (卫星或月球): 一律停止, 无需 currentNorad
      try {
        await fetch('/api/sat/track/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const targetLabel = { moon: '月球', sun: '太阳' }[trackingTarget];
        trackingTarget = 'sat';
        setTrackingUI(false);
        toast(targetLabel ? `已停止${targetLabel}跟踪` : '已停止跟踪');
      } catch (e) { toast('停止失败'); }
      return;
    }
    if (!currentNorad) { toast('请先选择卫星'); return; }
    try {
      const res = await fetch('/api/sat/track/' + currentNorad, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      if (data.ok && data.tracking) {
        trackingTarget = 'sat';
        setTrackingUI(true);
        toast('开始跟踪 ' + currentNorad);
      } else if (data.ok && data.detail) {
        // 俯仰归零中: 后台正在开环把俯仰降到 0°, 完成后自动开始跟踪
        trackingTarget = 'sat';
        setTrackingUI(true);
        toast(data.detail);
      } else toast('跟踪失败: ' + (data.detail || ''));
    } catch (e) { toast('跟踪失败'); }
  };

  // ===== 月球/太阳跟踪 (通用天体) =====
  let trackingTarget = 'sat';   // 'sat' | 'moon' | 'sun'

  function setTrackingUI(on, label) {
    tracking = on;
    $('btnTrack').textContent = on ? (label || '停止跟踪') : '开始跟踪';
    $('btnTrack').classList.toggle('tracking', on);
    // 同步移动端快捷按钮
    const btm = $('btnTrackM');
    if (btm) {
      btm.textContent = on ? '停止跟踪' : '开始跟踪';
      btm.classList.toggle('tracking', on);
    }
    $('btnMoonTrack').classList.toggle('active', on && trackingTarget === 'moon');
    $('btnSunTrack').classList.toggle('active', on && trackingTarget === 'sun');
  }

  const CELESTIAL = {
    moon: { emoji: '🌙', name: '月球' },
    sun:  { emoji: '☀️', name: '太阳' },
  };

  function renderCelestial(d, target) {
    if (!d) return;
    const c = CELESTIAL[target];
    $('satInfo').innerHTML =
      `<div class="row"><span class="k">目标</span><span class="v">${c.emoji} ${c.name}</span></div>` +
      `<div class="row"><span class="k">方位角</span><span class="v">${d.azimuth}°</span></div>` +
      `<div class="row"><span class="k">仰角</span><span class="v">${d.elevation}°</span></div>` +
      `<div class="row"><span class="k">距离</span><span class="v">${Math.round(d.distance)} km</span></div>` +
      `<div class="row"><span class="k">状态</span><span class="v" style="color:${d.visible ? '#4ade80' : '#facc15'}">${d.visible ? '地平线上' : '地平线下'}</span></div>`;
  }

  async function startCelestialTracking(target) {
    try {
      const c = CELESTIAL[target];
      const res = await fetch('/api/sat/track/' + target, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      if (!data.ok) { toast(c.name + '跟踪失败: ' + (data.detail || '')); return; }
      trackingTarget = target;
      setTrackingUI(true, `停止跟踪 ${c.emoji}`);
      setStreamTarget({ celestial: target });
      toast(data.detail || `开始${c.name}跟踪`);
    } catch (e) { toast(c.name + '跟踪失败'); }
  }

  function bindCelestialButton(btnId, target) {
    $(btnId).onclick = () => {
      if (tracking && trackingTarget === target) {
        // 已在跟踪该天体 -> 停止
        fetch('/api/sat/track/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        trackingTarget = 'sat';
        setTrackingUI(false);
        setStreamTarget(null);
        toast('已停止' + CELESTIAL[target].name + '跟踪');
      } else {
        startCelestialTracking(target);
      }
    };
  }
  bindCelestialButton('btnMoonTrack', 'moon');
  bindCelestialButton('btnSunTrack', 'sun');

  // ===== 刷新后恢复跟踪状态 (后端线程仍在跟踪) =====
  let pendingRestoreNorad = null;  // 跟踪中的卫星, 待收藏列表渲染后恢复选中视图
  async function restoreTracking() {
    try {
      const r = await fetch('/api/sat/track/status');
      const d = await r.json();
      if (!d.ok) return false;
      if (d.tracking && CELESTIAL[d.norad]) {
        // 恢复月球/太阳跟踪状态
        const c = CELESTIAL[d.norad];
        trackingTarget = d.norad;
        setTrackingUI(true, `停止跟踪 ${c.emoji}`);
        setStreamTarget({ celestial: d.norad });
        toast(c.name + '跟踪继续进行中');
        return true;  // 天体跟踪, 不自动选卫星
      }
      if (d.tracking && d.norad) {
        tracking = true;
        $('btnTrack').textContent = '停止跟踪';
        $('btnTrack').classList.add('tracking');
        const btm = $('btnTrackM');
        if (btm) { btm.textContent = '停止跟踪'; btm.classList.add('tracking'); }
        // 记录待恢复卫星, 收藏列表渲染完成后自动选中 (原实现遍历空 DOM 无法恢复)
        pendingRestoreNorad = d.norad;
        toast('跟踪继续进行中 (' + d.norad + ')');
        return true;
      }
      return false;
    } catch (e) { return false; }
  }

  // ===== SSE 推流: 一条连接接收全部状态, 替代多路 HTTP 轮询 =====
  // 事件: state(1s, 串口状态/云台位置/编码器/目标位置) serial(0.8s)
  //       favorites(30s) photocalib(1s); 目标位置由连接参数 norad/celestial 决定
  let streamEs = null;
  let streamTarget = null;   // {norad} / {celestial} / null

  // ===== 天体地图图层更新 =====
  // 晨昏线 = 以太阳星下点为极点的大圆 (90° 处), 用球面正交基生成整圈点
  function terminatorPoints(sunLat, sunLon, n) {
    n = n || 180;
    const rad = d => d * Math.PI / 180;
    const la = rad(sunLat), lo = rad(sunLon);
    const v = [Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)];
    const cross = (u, w) => [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]];
    let a = cross(v, [0, 0, 1]);
    let al = Math.hypot(a[0], a[1], a[2]);
    if (al < 1e-9) { a = cross(v, [1, 0, 0]); al = Math.hypot(a[0], a[1], a[2]); }
    a = [a[0] / al, a[1] / al, a[2] / al];
    const b = cross(v, a);
    const bl = Math.hypot(b[0], b[1], b[2]);
    const bn = [b[0] / bl, b[1] / bl, b[2] / bl];
    const pts = [];
    for (let i = 0; i < n; i++) {
      const t = 2 * Math.PI * i / n;
      const c = Math.cos(t), s = Math.sin(t);
      const p = [a[0] * c + bn[0] * s, a[1] * c + bn[1] * s, a[2] * c + bn[2] * s];
      pts.push([Math.asin(p[2]) * 180 / Math.PI, Math.atan2(p[1], p[0]) * 180 / Math.PI]);
    }
    return pts;
  }

  function updateCelOverlays(cel) {
    if (!cel) return;
    if (cel.moon && cel.moon.sub_lat !== null && cel.moon.sub_lon !== null) {
      const p = [cel.moon.sub_lat, cel.moon.sub_lon];
      moonMarker.setLatLng(p).setOpacity(1);
      moonVisCircle.setLatLng(p).setStyle({ opacity: 1, fillOpacity: 0.06 });
    }
    if (cel.sun && cel.sun.sub_lat !== null && cel.sun.sub_lon !== null) {
      sunMarker.setLatLng([cel.sun.sub_lat, cel.sun.sub_lon]).setOpacity(1);
      termLine.setLatLngs(terminatorPoints(cel.sun.sub_lat, cel.sun.sub_lon)).setStyle({ opacity: 0.85 });
    }
  }

  let streamWs = null;
  let wsUsing = false;

  // 统一消息分发: 供 WebSocket 与 SSE 共用, 保证两种通道渲染一致
  function onStreamFrame(name, d) {
    try {
      if (name === 'state') {
        if (window.renderStatus) {
          // rotctld_connected 是 state 帧顶层字段, 合并进 status 再渲染 (否则恒显离线)
          window.renderStatus(Object.assign({}, d.status, { rotctld_connected: !!d.rotctld_connected }));
        }
        // 跟踪状态实时同步: 后端已自动停止(出境/超时)时复位前端按钮。
        // 不依赖一次性 alert(弱网下可能丢失), 每帧 status.tracking 为准
        if (d.status && typeof d.status.tracking === 'boolean' && d.status.tracking === false && tracking) {
          trackingTarget = 'sat';
          setTrackingUI(false);
        }
        if (window.renderPosition) window.renderPosition(d.pos);
        if (d.enc) applyEncFrame(d.enc);          // 角度/方向线/编码器面板: 实时驱动
        if (d.cel) updateCelOverlays(d.cel);
        if (d.sat && d.sat_kind === 'sat') renderSatState(d.sat, d.enc);
        else if (d.sat) renderCelestial(d.sat, d.sat_kind);
        // 跟踪自动停止提醒 (一次性): 同步前端跟踪状态, 确认后复位回 0°
        if (d.alert) {
          if (tracking) { trackingTarget = 'sat'; setTrackingUI(false); }
          const reason = d.alert === 'timeout' ? '跟踪超时，已自动停止' : '卫星已出境，已自动停止跟踪';
          if (confirm(reason + '，云台已停止。是否返回 0°？')) {
            api('/api/reset', {}).then(x => {
              if (x.abort) toast('无法复位: ' + (x.detail || ''));
              else toast('复位中，请稍候...');
            }).catch(() => toast('复位请求失败'));
          }
        }
      } else if (name === 'serial') {
        if (window.renderSerialLog) window.renderSerialLog(d);
      } else if ( name === 'favorites') {
        renderFavorites(d);
      } else if (name === 'photocalib') {
        renderPhotoCalib(d);
      }
    } catch (err) { /* 忽略 */ }
  }

  // 通道不可用的兜底 SSE (功能永不中断)
  function openStreamSse(target) {
    let url = '/api/stream';
    if (target && target.norad != null) url += '?norad=' + encodeURIComponent(target.norad);
    else if (target && target.celestial) url += '?celestial=' + target.celestial;
    streamEs = new EventSource(url);
    streamEs.addEventListener('state', e => onStreamFrame('state', JSON.parse(e.data)));
    streamEs.addEventListener('serial', e => onStreamFrame('serial', JSON.parse(e.data)));
    streamEs.addEventListener('favorites', e => onStreamFrame('favorites', JSON.parse(e.data)));
    streamEs.addEventListener('photocalib', e => onStreamFrame('photocalib', JSON.parse(e.data)));
    streamEs.onerror = () => { if (window.onStreamDown) window.onStreamDown(); };
    streamEs.onopen = () => { loadFavorites(); };  // 重连后立即刷新收藏, 不等 30s
  }

  // 尝试 WebSocket(优先, 低延迟); 3s 未连通或中途断开 → 自动回退 SSE
  function tryWs(baseWs, target) {
    let url = baseWs;
    if (!/\/ws([?]|$)/.test(url)) url += '/ws';
    const q = [];
    if (target && target.norad != null) q.push('norad=' + encodeURIComponent(target.norad));
    else if (target && target.celestial) q.push('celestial=' + target.celestial);
    if (q.length) url += (url.includes('?') ? '&' : '?') + q.join('&');
    let ws;
    try { ws = new WebSocket(url); } catch (e) { openStreamSse(target); return; }
    streamWs = ws;
    wsUsing = false;
    const tmo = setTimeout(() => {
      if (!wsUsing) { try { ws.close(); } catch (e) {} openStreamSse(target); }
    }, 3000);
    ws.onopen = () => { wsUsing = true; clearTimeout(tmo); if (window.onStreamDown) window.onStreamDown(); loadFavorites(); };
    ws.onmessage = ev => { wsUsing = true; const m = JSON.parse(ev.data); onStreamFrame(m.t, m.data); };
    ws.onclose = () => {
      clearTimeout(tmo);
      if (streamWs === ws) { streamWs = null; if (wsUsing) openStreamSse(target); }
    };
    ws.onerror = () => {};
  }

  function openStream() {
    if (streamEs) { streamEs.close(); streamEs = null; }
    if (streamWs) { try { streamWs.onclose = null; streamWs.close(); } catch (e) {} streamWs = null; }
    const target = streamTarget;
    // 先查 WS 端点; 未配置/连不上则回退 SSE
    fetch('/api/wsurl').then(r => r.json()).then(d => {
      if (streamTarget !== target) return;      // 目标已在等待期间变更, 丢弃
      if (d && d.ok && d.ws) tryWs(d.ws, target);
      else openStreamSse(target);
    }).catch(() => openStreamSse(target));
  }

  function setStreamTarget(target) {
    // target: {norad} / {celestial} / null; 目标未变则不重建连接
    const same = streamTarget && target &&
      String(streamTarget.norad) === String(target.norad) &&
      streamTarget.celestial === target.celestial;
    streamTarget = target;
    if (!same) openStream();
  }

  // ===== 初始化 =====
  initMap();
  bindGridLabelRefresh();
  bindTrackDurBtns();
  loadEncCal();
  openStream();
  setTimeout(applyGridSettings, 1500);   // 页面加载后按设置绘制网格/VUCC 层
  // 先确认后端跟踪状态 (决定恢复哪颗卫星), 再渲染收藏列表并恢复选中
  (async () => {
    await restoreTracking();
    loadFavorites();
  })();

  // ===== 编码器角度: 由 SSE state 帧驱动 (内网/外网统一一条流) =====
  // 外网经 Cloudflare Tunnel 时每个 HTTP 请求要 0.5~3.5s 往返, 轮询会堆积成"卡死";
  // SSE 长连接 0.4s 一帧, 天然实时, 且前端无需再管乱序/重连。
  function applyEncFrame(enc) {
    if (!enc) return;
    if (window.renderEncoder) window.renderEncoder(enc);   // 编码器面板 + 方向线
    updateEncCalUi(enc);   // 编码器标定面板
    // 大数字角度实时刷新: dpan 每个 UDP 包都更新, 复位/转动时实时跟随
    let pv = (enc.dpan !== null && enc.dpan !== undefined) ? Number(enc.dpan)
           : ((enc.pan !== null && enc.pan !== undefined) ? Number(enc.pan) : NaN);
    if (pv >= 359.95) pv = 0.0;
    if (!isNaN(pv)) $('panVal').innerHTML = pv.toFixed(1) + '<span class="unit">°</span>';
    // 俯仰角同样实时刷新
    if (enc.tilt !== null && enc.tilt !== undefined) {
      $('tiltVal').innerHTML = Number(enc.tilt).toFixed(1) + '<span class="unit">°</span>';
    }
  }

  // ===== 地图云台方向指示线（IIFE 闭包内，直接访问 map / obsMarker） =====
  let ptzDirLine = null, ptzArrow = null;
  function ptzDirEnd(lat, lon, bearingDeg, distKm) {
    const R = 6371, brng = bearingDeg * Math.PI / 180;
    const d = distKm / R, r1 = lat * Math.PI / 180, r2 = lon * Math.PI / 180;
    const lt = Math.asin(Math.sin(r1) * Math.cos(d) + Math.cos(r1) * Math.sin(d) * Math.cos(brng));
    const ln = r2 + Math.atan2(Math.sin(brng) * Math.sin(d) * Math.cos(r1), Math.cos(d) - Math.sin(r1) * Math.sin(lt));
    return [lt * 180 / Math.PI, ln * 180 / Math.PI];
  }

  // 两点的局部方位角 (大圆切线方向)
  function bearingBetween(lat1, lon1, lat2, lon2) {
    const r = Math.PI / 180;
    const y = Math.sin((lon2 - lon1) * r) * Math.cos(lat2 * r);
    const x = Math.cos(lat1 * r) * Math.sin(lat2 * r) - Math.sin(lat1 * r) * Math.cos(lat2 * r) * Math.cos((lon2 - lon1) * r);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }

  // 大圆航路点: 从观测站沿 bearingDeg 前进 distKm, 插值 n 段 (起点方位准确, 长线不偏)
  function greatCirclePoints(oLat, oLon, bearingDeg, distKm, n) {
    const R = 6371, brng = bearingDeg * Math.PI / 180;
    const r1 = oLat * Math.PI / 180, r2 = oLon * Math.PI / 180;
    const total = distKm / R;
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const d = total * i / n;
      const lt = Math.asin(Math.sin(r1) * Math.cos(d) + Math.cos(r1) * Math.sin(d) * Math.cos(brng));
      const ln = r2 + Math.atan2(Math.sin(brng) * Math.sin(d) * Math.cos(r1), Math.cos(d) - Math.sin(r1) * Math.sin(lt));
      pts.push([lt * 180 / Math.PI, ln * 180 / Math.PI]);
    }
    return pts;
  }

  window.updatePtzDir = function(panDeg) {
    if (!map || !obsMarker || isNaN(panDeg)) { console.log('ptzDir skip', {obsMarker, map, panDeg}); return; }
    const pos = obsMarker.getLatLng(), o = [pos.lat, pos.lng];
    const pts = greatCirclePoints(o[0], o[1], panDeg, 8000, 32);
    const end = pts[pts.length - 1];
    // 箭头: 用终点处大圆切线方位角对齐 (长线端点方位 ≠ 起点方位)
    const prev = pts[pts.length - 2];
    const endBrng = bearingBetween(prev[0], prev[1], end[0], end[1]);
    const back = ptzDirEnd(end[0], end[1], (endBrng + 180) % 360, 30);
    const l1 = ptzDirEnd(back[0], back[1], (endBrng + 270) % 360, 15);
    const l2 = ptzDirEnd(back[0], back[1], (endBrng + 90) % 360, 15);
    if (!ptzDirLine) {
      ptzDirLine = L.polyline(pts, { color: '#ff4444', weight: 2.5, opacity: 1 }).addTo(map);
      ptzArrow = L.polygon([end, l1, l2], { color: '#ff4444', fillColor: '#ff4444', fillOpacity: 1, weight: 0 }).addTo(map);
      ptzDirLine.bringToFront();   // 仅创建时置顶一次; 每 tick 调 bringToFront 会强制重绘整个 pane 造成闪烁
      ptzArrow.bringToFront();
    } else {
      ptzDirLine.setLatLngs(pts);
      ptzArrow.setLatLngs([end, l1, l2]);
    }
  };
})();
