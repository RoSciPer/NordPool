// =====================================================================
//  Nord Pool Shelly Boiler Control  --  v3.7.1
// =====================================================================
//  Grafiki = ikdienas atkārtojums (* * *), BEZ konkrēta datuma.
//  Auto-sync: VIENREIZ vakarā (20:00), retry tikai ja neizdevās.
//  Poga / TG /sync — vienmēr atjauno. Pēc veiksmīga sync — vairs nepārraksta.
// =====================================================================

var SERVER_URL   = "{{SERVER_URL}}";
var DEVICE_TOKEN = "{{DEVICE_TOKEN}}";
var SWITCH_ID    = {{SWITCH_ID}};

var POLL_INTERVAL_MS       = 60 * 1000;
var AUTO_SYNC_AFTER_HOUR   = 20;
var WEEKLY_UPTIME_SEC      = 7 * 24 * 3600;
var SYNC_BUSY              = false;
var AUTO_LOCKED            = false;
var LAST_PLAN_DATE         = "";
var LAST_SYNC_DAY          = "";

function log(msg) {
  print("[NP-Boiler] " + msg);
}

function maySync() {
  return new Date().getHours() >= AUTO_SYNC_AFTER_HOUR;
}

function isManualSync(reason) {
  return reason === "button" || reason === "server-refresh";
}

function todayKey() {
  var d = new Date();
  var m = d.getMonth() + 1;
  var day = d.getDate();
  return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
}

function refreshDailyLock() {
  var t = todayKey();
  if (LAST_SYNC_DAY !== t) {
    LAST_SYNC_DAY = t;
    AUTO_LOCKED = false;
    LAST_PLAN_DATE = "";
  }
}

function markSynced(planDate) {
  LAST_PLAN_DATE = planDate || "";
  LAST_SYNC_DAY = todayKey();
  AUTO_LOCKED = true;
}

function markFailed() {
  AUTO_LOCKED = false;
  LAST_PLAN_DATE = "";
}

function shouldSkipAutoSync(reason, planDate) {
  if (isManualSync(reason)) return false;
  if (reason === "boot-empty") return false;
  refreshDailyLock();
  if (!AUTO_LOCKED) return false;
  if (planDate && LAST_PLAN_DATE && LAST_PLAN_DATE !== planDate) return false;
  return true;
}

function httpGet(url, cb) {
  Shelly.call("HTTP.GET", { url: url, timeout: 30 }, cb);
}

function httpPost(url, body, cb) {
  Shelly.call("HTTP.Request", {
    method: "POST",
    url: url,
    headers: { "Content-Type": "application/json" },
    body: body,
    timeout: 20
  }, cb);
}

function listJobs(res) {
  if (res && res.jobs) return res.jobs;
  if (res && res.length !== undefined) return res;
  return [];
}

function hasAnyHeatingSchedules(cb) {
  Shelly.call("Schedule.List", {}, function(res, err) {
    if (err !== 0) { cb(false); return; }
    var jobs = listJobs(res);
    for (var i = 0; i < jobs.length; i++) {
      var nm = jobs[i].name || "";
      if (nm.indexOf("np-d") === 0 || nm.indexOf("np-fb-") === 0) {
        cb(true);
        return;
      }
    }
    cb(false);
  });
}

function reportInstalled(okCount, total, meta) {
  httpPost(
    SERVER_URL + "/api/v1/report_schedule?token=" + DEVICE_TOKEN,
    JSON.stringify({
      target_date: meta.target_date,
      ok_count: okCount,
      total: total,
      note: meta.note,
      schedule_kind: meta.schedule_kind
    }),
    function() {}
  );
}

function installSchedules(schedules, meta) {
  if (!schedules || schedules.length === 0) {
    log("Nothing to install — keeping existing.");
    SYNC_BUSY = false;
    return;
  }

  Shelly.call("Schedule.DeleteAll", {}, function(res, delErr) {
    if (delErr !== 0) {
      log("DeleteAll error: " + delErr);
      markFailed();
      SYNC_BUSY = false;
      return;
    }

    var idx = 0;
    var okCount = 0;
    log("Installing " + schedules.length + " daily (" + meta.schedule_kind + ")");

    function createNext() {
      if (idx >= schedules.length) {
        if (okCount === schedules.length) {
          log("OK " + okCount + "/" + schedules.length);
          markSynced(meta.target_date || "");
          reportInstalled(okCount, schedules.length, meta);
        } else {
          log("WARN partial install " + okCount + "/" + schedules.length);
          markFailed();
        }
        SYNC_BUSY = false;
        return;
      }
      var entry = schedules[idx];
      Shelly.call("Schedule.Create", entry, function(cres, err, errMsg) {
        if (err !== 0) log("FAIL #" + idx + " " + (entry.timespec || "") + " " + errMsg);
        else { okCount++; log("OK " + entry.name + " " + entry.timespec); }
        idx++;
        createNext();
      });
    }
    createNext();
  });
}

function clearSchedules() {
  Shelly.call("Schedule.DeleteAll", {}, function() {
    log("Vacation: cleared.");
    markFailed();
    SYNC_BUSY = false;
  });
}

function fetchInstall(scope, cb) {
  var url = SERVER_URL + "/api/v1/install_schedules?token=" + DEVICE_TOKEN
    + "&id=" + SWITCH_ID + "&scope=" + scope;
  httpGet(url, function(res, err_code) {
    if (err_code !== 0 || !res || res.code !== 200) { cb(null); return; }
    try { cb(JSON.parse(res.body)); } catch (e) { cb(null); }
  });
}

function applyData(data, reason, kind) {
  var schedules = kind === "fallback" ? data.fallback_schedules : data.schedules;
  if (!schedules || schedules.length === 0) {
    log("No " + kind + " — keeping existing schedules.");
    SYNC_BUSY = false;
    return;
  }
  if (data.ranges) {
    for (var i = 0; i < data.ranges.length; i++) {
      if (data.ranges[i].label) log("  " + data.ranges[i].label + " (katru dienu)");
    }
  }
  installSchedules(schedules, {
    target_date: data.target_date,
    note: reason,
    schedule_kind: kind
  });
}

function syncSchedule(reason) {
  if (SYNC_BUSY) return;
  if (!maySync()) {
    log("Blocked until " + AUTO_SYNC_AFTER_HOUR + ":00 (" + reason + ")");
    return;
  }

  SYNC_BUSY = true;
  log("=== SYNC " + reason + " ===");

  fetchInstall("tomorrow", function(data) {
    if (!data) {
      log("HTTP fail — keeping existing daily schedules.");
      markFailed();
      SYNC_BUSY = false;
      return;
    }

    if (shouldSkipAutoSync(reason, data.target_date)) {
      log("Already synced tonight — skip (" + reason + ")");
      SYNC_BUSY = false;
      return;
    }

    if (data.vacation_mode) {
      clearSchedules();
      return;
    }
    if (data.tomorrow_ready && data.schedules && data.schedules.length > 0) {
      log("Daily recurring from plan " + data.target_date);
      applyData(data, reason, "daily");
      return;
    }
    if (data.fallback_schedules && data.fallback_schedules.length > 0) {
      log("No tomorrow prices — FALLBACK weekly");
      applyData(data, "fallback", "fallback");
      return;
    }
    log("No new data — keeping existing.");
    markFailed();
    SYNC_BUSY = false;
  });
}

function checkRefreshPending() {
  if (!maySync()) return;
  httpGet(SERVER_URL + "/api/v1/refresh_pending?token=" + DEVICE_TOKEN, function(res, err) {
    if (err !== 0 || !res || res.code !== 200) return;
    try {
      var body = JSON.parse(res.body);
      if (body && body.pull_schedule) syncSchedule("server-refresh");
    } catch (e) {}
  });
}

function maybeNightlySync() {
  refreshDailyLock();
  if (!maySync() || AUTO_LOCKED) return;
  var h = new Date().getHours();
  var m = new Date().getMinutes();
  if (h < 20 || h > 23) return;
  if (m !== 0 && m !== 30) return;
  syncSchedule(h === 20 && m === 0 ? "nightly" : "retry");
}

function maybeWeeklyReboot() {
  Shelly.call("Sys.GetStatus", {}, function(st, err) {
    if (err !== 0 || !st) return;
    if ((st.uptime || 0) >= WEEKLY_UPTIME_SEC) {
      log("Reboot " + Math.floor(st.uptime / 86400) + "d uptime");
      Shelly.call("Sys.Reboot", { delay_ms: 5000 });
    }
  });
}

function bootSafetyCheck() {
  refreshDailyLock();
  hasAnyHeatingSchedules(function(has) {
    if (has) {
      log("Schedules present on boot — auto sync locked.");
      AUTO_LOCKED = true;
      LAST_SYNC_DAY = todayKey();
      return;
    }
    if (!maySync()) {
      log("No schedules — waiting for " + AUTO_SYNC_AFTER_HOUR + ":00 sync.");
      return;
    }
    log("No schedules on boot — sync now.");
    syncSchedule("boot-empty");
  });
}

Shelly.addEventHandler(function(ev) {
  if (!ev || !ev.component || !ev.info) return;
  if (ev.component === "input:0" && ev.info.event === "long_push") {
    syncSchedule("button");
  }
});

Timer.set(60 * 60 * 1000, true, function() {
  httpPost(SERVER_URL + "/api/v1/heartbeat?token=" + DEVICE_TOKEN, "{}", function() {});
  maybeWeeklyReboot();
});

Timer.set(POLL_INTERVAL_MS, true, function() {
  checkRefreshPending();
  maybeNightlySync();
});

log("v3.7.1 — vienreiz vakara, manual ar pogu/TG");
bootSafetyCheck();
