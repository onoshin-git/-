/**
 * Lv1 カリキュラム実行 - メインアプリケーションロジック
 * セッション管理、出題→回答→採点→レビューのフロー制御
 * 全ステップ完了時のみ /lv1/complete を呼び出してDB保存
 *
 * 改善点:
 * - サーバー連動タイマー（mm:ss表示、改ざん対策）
 * - score_breakdown表示
 * - Lv1体系図（taxonomy）表示
 */
const Lv1App = (() => {
  // --- セッション管理 ---

  const SESSION_KEY = "ai_levels_session";

  function generateUUID() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function getSession() {
    try {
      const raw = sessionStorage.getItem(SESSION_KEY);
      if (raw) return JSON.parse(raw);
    } catch { /* ignore */ }
    const session = {
      session_id: generateUUID(),
      current_step: 0,
      questions: [],
      answers: [],
      grades: [],
      started_at: new Date().toISOString(),
    };
    saveSession(session);
    return session;
  }

  function saveSession(session) {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  }

  // --- タイマー管理 ---

  let timerInterval = null;
  let questionStartedAtMs = null;
  let serverTimeDeltaMs = 0;
  let timerGeneration = 0;

  async function syncServerTime() {
    try {
      const data = await ApiClient.getServerTime();
      const clientNow = Date.now();
      serverTimeDeltaMs = data.server_time_ms - clientNow;
    } catch (e) {
      console.warn("Failed to sync server time, using client time:", e);
      serverTimeDeltaMs = 0;
    }
  }

  function getServerNowMs() {
    return Date.now() + serverTimeDeltaMs;
  }

  async function startTimer(sessionId, step) {
    stopTimer();
    const myGen = ++timerGeneration;
    questionStartedAtMs = getServerNowMs(); // 即座にフォールバック値を設定（API完了前の送信対策）
    try {
      const data = await ApiClient.startQuestion(sessionId, step);
      if (myGen !== timerGeneration) return; // stale: 別のstartTimerが開始済み
      questionStartedAtMs = data.started_at_ms;
    } catch (e) {
      if (myGen !== timerGeneration) return;
      console.warn("Failed to start question timer:", e);
      questionStartedAtMs = getServerNowMs();
    }

    if (myGen !== timerGeneration) return;
    const timerEl = document.getElementById("question-timer");
    if (timerEl) {
      timerEl.hidden = false;
      timerEl.textContent = "00:00";
    }

    timerInterval = setInterval(() => {
      const elapsed = getServerNowMs() - questionStartedAtMs;
      const secs = Math.floor(elapsed / 1000);
      const mm = String(Math.floor(secs / 60)).padStart(2, "0");
      const ss = String(secs % 60).padStart(2, "0");
      if (timerEl) timerEl.textContent = mm + ":" + ss;
    }, 200);
  }

  function stopTimer() {
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
    if (questionStartedAtMs) {
      const elapsed = getServerNowMs() - questionStartedAtMs;
      questionStartedAtMs = null; // リセットして前の設問の値が残らないようにする
      return elapsed;
    }
    return null;
  }

  // --- DOM参照 ---

  const els = {};

  function cacheDom() {
    els.loading = document.getElementById("section-loading");
    els.questionSection = document.getElementById("section-question");
    els.resultSection = document.getElementById("section-result");
    els.finalSection = document.getElementById("section-final");
    els.progressFill = document.getElementById("progress-fill");
    els.progressLabel = document.getElementById("progress-label");
    els.progressBar = document.getElementById("progress-bar");
    els.questionStep = document.getElementById("question-step");
    els.questionType = document.getElementById("question-type");
    els.questionContext = document.getElementById("question-context");
    els.questionPrompt = document.getElementById("question-prompt");
    els.questionOptions = document.getElementById("question-options");
    els.textareaWrap = document.getElementById("textarea-wrap");
    els.answerText = document.getElementById("answer-text");
    els.btnSubmit = document.getElementById("btn-submit");
    els.resultVerdict = document.getElementById("result-verdict");
    els.resultScore = document.getElementById("result-score");
    els.resultFeedback = document.getElementById("result-feedback");
    els.resultExplanation = document.getElementById("result-explanation");
    els.btnNext = document.getElementById("btn-next");
    els.finalIcon = document.getElementById("final-icon");
    els.finalTitle = document.getElementById("final-title");
    els.finalMessage = document.getElementById("final-message");
    els.finalSummary = document.getElementById("final-summary");
  }

  // --- セクション表示制御 ---

  function showSection(name) {
    els.loading.hidden = name !== "loading";
    els.questionSection.hidden = name !== "question";
    els.resultSection.hidden = name !== "result";
    els.finalSection.hidden = name !== "final";
  }

  function updateProgress(current, total) {
    const pct = total > 0 ? Math.round((current / total) * 100) : 0;
    els.progressFill.style.width = pct + "%";
    els.progressLabel.textContent = "\u30b9\u30c6\u30c3\u30d7 " + current + " / " + total;
    els.progressBar.setAttribute("aria-valuenow", pct);
  }

  // --- 設問タイプのラベル ---

  const TYPE_LABELS = {
    multiple_choice: "選択問題",
    free_text: "自由記述",
    scenario: "シナリオ",
  };

  // --- 設問表示 ---

  function renderQuestion(question, stepIndex, totalSteps) {
    updateProgress(stepIndex + 1, totalSteps);

    els.questionStep.textContent = "\u30b9\u30c6\u30c3\u30d7 " + question.step;
    els.questionType.textContent = TYPE_LABELS[question.type] || question.type;
    els.questionPrompt.textContent = question.prompt;

    if (question.context) {
      els.questionContext.textContent = question.context;
      els.questionContext.hidden = false;
    } else {
      els.questionContext.hidden = true;
    }

    if (question.type === "multiple_choice" && question.options) {
      els.questionOptions.innerHTML = "";
      question.options.forEach((opt, i) => {
        const label = document.createElement("label");
        label.className = "option-label";
        label.innerHTML =
          '<input type="radio" name="mc-answer" value="' + i + '">' +
          '<span class="option-text">' + escapeHtml(opt) + '</span>';
        els.questionOptions.appendChild(label);
      });
      els.questionOptions.hidden = false;
      els.textareaWrap.hidden = true;
    } else {
      els.questionOptions.hidden = true;
      els.textareaWrap.hidden = false;
      els.answerText.value = "";
    }

    els.btnSubmit.disabled = true;
    showSection("question");

    // タイマー開始（サーバーに記録）
    startTimer(session.session_id, question.step);
  }

  function escapeHtml(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }

  // --- 回答取得 ---

  function getAnswer(question) {
    if (question.type === "multiple_choice" && question.options) {
      const checked = document.querySelector('input[name="mc-answer"]:checked');
      return checked ? question.options[parseInt(checked.value, 10)] : null;
    }
    return els.answerText.value.trim() || null;
  }

  // --- 入力バリデーション ---

  function setupInputListeners() {
    els.questionOptions.addEventListener("change", () => {
      els.btnSubmit.disabled = false;
    });
    els.answerText.addEventListener("input", () => {
      els.btnSubmit.disabled = els.answerText.value.trim().length === 0;
    });
  }

  // --- 採点結果表示 ---

  function renderResult(gradeResult) {
    if (gradeResult.passed) {
      els.resultVerdict.textContent = "\u2705 合格";
      els.resultVerdict.className = "result-card__verdict result-card__verdict--passed";
    } else {
      els.resultVerdict.textContent = "\u274c 不合格";
      els.resultVerdict.className = "result-card__verdict result-card__verdict--failed";
    }
    els.resultScore.textContent = "スコア: " + gradeResult.score + " / 100";
    els.resultFeedback.textContent = gradeResult.feedback || "";
    els.resultExplanation.textContent = gradeResult.explanation || "";

    // score_breakdown 表示（前回データのリセット含む）
    const breakdownEl = document.getElementById("result-score-breakdown");
    if (breakdownEl) breakdownEl.hidden = true;
    if (breakdownEl && gradeResult.score_breakdown) {
      const bd = gradeResult.score_breakdown;
      const labels = {
        intent_understanding: "意図理解",
        coverage: "要点網羅",
        structure: "構造化",
        practical_relevance: "実務妥当性",
      };
      let html = '<div class="score-breakdown">';
      for (const [key, label] of Object.entries(labels)) {
        const val = bd[key] || 0;
        const pct = (val / 25) * 100;
        html += '<div class="score-breakdown__item">';
        html += '<span class="score-breakdown__label">' + label + '</span>';
        html += '<span class="score-breakdown__value">' + val + '/25</span>';
        html += '<div class="score-breakdown__bar"><div class="score-breakdown__fill" style="width:' + pct + '%"></div></div>';
        html += '</div>';
      }
      html += "</div>";
      breakdownEl.innerHTML = html;
      breakdownEl.hidden = false;
    }

    // 回答時間表示（前回データのリセット含む）
    const timeEl = document.getElementById("result-response-time");
    if (timeEl) timeEl.hidden = true;
    if (timeEl && gradeResult.response_time_ms) {
      const secs = Math.round(gradeResult.response_time_ms / 1000);
      const mm = Math.floor(secs / 60);
      const ss = secs % 60;
      let timeText = "回答時間: " + mm + "分" + ss + "秒";
      if (gradeResult.speed_label) {
        const speedLabels = { fast: "\u26a1 高速", mid: "\u2713 標準", slow: "\u23f3 低速" };
        timeText += " (" + (speedLabels[gradeResult.speed_label] || gradeResult.speed_label) + ")";
      }
      timeEl.textContent = timeText;
      timeEl.hidden = false;
    }

    showSection("result");
  }

  // --- Lv1 体系図データ（④） ---

  const LV1_TAXONOMY = {
    title: "LEVEL 1: AI Fundamentals",
    criteria: "AIで何ができる/できないかを顧客に説明できる",
    topics: [
      {
        category: "AI/ML/DLの基礎",
        items: [
          "AI（人工知能）の定義と歴史",
          "機械学習（ML）の仕組みと種類",
          "ディープラーニング（DL）の特徴",
          "AI・ML・DLの階層関係",
        ],
      },
      {
        category: "生成AI・LLM",
        items: [
          "大規模言語モデル（LLM）の概要",
          "生成AIの主要ユースケース",
          "プロンプトエンジニアリングの基本",
          "生成AIの得意・不得意",
        ],
      },
      {
        category: "AIの限界と倫理",
        items: [
          "AIが苦手なタスクの理解",
          "ハルシネーション（幻覚）リスク",
          "バイアスと公平性の問題",
          "プライバシーとデータ保護",
        ],
      },
      {
        category: "実務活用",
        items: [
          "顧客へのAI説明スキル",
          "業務プロセスへのAI適用判断",
          "AIツール選定の基本的な考え方",
          "AI導入時のリスク評価",
        ],
      },
    ],
  };

  // --- 最終結果表示 ---

  function renderFinal(session) {
    const passedCount = session.grades.filter((g) => g.passed).length;
    const totalSteps = session.questions.length;
    const allPassed = passedCount === totalSteps;

    els.finalIcon.textContent = allPassed ? "\ud83c\udf89" : "\ud83d\udcdd";
    els.finalTitle.textContent = allPassed ? "Lv1 合格！" : "Lv1 結果";
    els.finalMessage.textContent = allPassed
      ? "おめでとうございます！全ステップに合格しました。"
      : passedCount + " / " + totalSteps + " ステップに合格しました。";

    let summaryHtml = "";
    session.questions.forEach((q, i) => {
      const g = session.grades[i];
      const icon = g && g.passed ? "\u2705" : "\u274c";
      const score = g ? g.score : "-";
      let extra = "";
      if (g && g.response_time_ms) {
        const secs = Math.round(g.response_time_ms / 1000);
        extra = " (" + Math.floor(secs / 60) + "分" + (secs % 60) + "秒)";
      }
      summaryHtml +=
        '<div class="summary-row">' +
        '<span>ステップ ' + q.step + '</span>' +
        '<span>' + icon + ' ' + score + '点' + extra + '</span>' +
        '</div>';
    });
    els.finalSummary.innerHTML = summaryHtml;

    renderTaxonomy();

    updateProgress(totalSteps, totalSteps);
    showSection("final");
  }

  function renderTaxonomy() {
    const container = document.getElementById("lv1-taxonomy");
    if (!container) return;

    let html = '<div class="taxonomy">';
    html += '<h3 class="taxonomy__title">' + escapeHtml(LV1_TAXONOMY.title) + '</h3>';
    html += '<p class="taxonomy__criteria"><strong>到達基準:</strong> ' + escapeHtml(LV1_TAXONOMY.criteria) + '</p>';
    html += '<div class="taxonomy__grid">';

    LV1_TAXONOMY.topics.forEach((topic) => {
      html += '<div class="taxonomy__card">';
      html += '<h4 class="taxonomy__card-title">' + escapeHtml(topic.category) + '</h4>';
      html += '<ul class="taxonomy__list">';
      topic.items.forEach((item) => {
        html += '<li>' + escapeHtml(item) + '</li>';
      });
      html += '</ul></div>';
    });

    html += '</div></div>';
    container.innerHTML = html;
    container.hidden = false;
  }

  // --- メインフロー ---

  let session = null;

  async function start() {
    cacheDom();
    setupInputListeners();
    session = getSession();

    await syncServerTime();

    if (session.questions.length > 0 && session.current_step < session.questions.length) {
      renderQuestion(session.questions[session.current_step], session.current_step, session.questions.length);
      return;
    }

    if (session.questions.length > 0 && session.current_step >= session.questions.length) {
      renderFinal(session);
      return;
    }

    showSection("loading");
    try {
      ApiClient.hideError();
      const data = await ApiClient.generate(session.session_id);
      session.questions = data.questions || [];
      session.current_step = 0;
      saveSession(session);

      if (session.questions.length === 0) {
        ApiClient.showError("設問の生成に失敗しました。", () => start());
        return;
      }

      renderQuestion(session.questions[0], 0, session.questions.length);
    } catch (err) {
      showSection("loading");
      ApiClient.showError(
        "テスト・ドリルの生成に失敗しました。ネットワーク接続を確認してください。",
        () => start()
      );
    }
  }

  async function submitAnswer() {
    const question = session.questions[session.current_step];
    const answer = getAnswer(question);
    if (!answer) return;

    const elapsedMs = stopTimer();

    els.btnSubmit.disabled = true;
    els.btnSubmit.textContent = "採点中...";

    try {
      ApiClient.hideError();
      const result = await ApiClient.grade(
        session.session_id,
        question.step,
        question,
        answer,
        typeof elapsedMs === "number" ? Math.round(elapsedMs) : undefined
      );

      session.answers.push(answer);
      session.grades.push(result);
      saveSession(session);

      renderResult(result);
    } catch (err) {
      els.btnSubmit.disabled = false;
      els.btnSubmit.textContent = "回答を送信";
      ApiClient.showError(
        "採点に失敗しました。もう一度お試しください。",
        () => submitAnswer()
      );
    }
  }

  async function nextStep() {
    session.current_step += 1;
    saveSession(session);

    if (session.current_step >= session.questions.length) {
      await completeSession();
    } else {
      els.btnSubmit.textContent = "回答を送信";
      renderQuestion(
        session.questions[session.current_step],
        session.current_step,
        session.questions.length
      );
    }
  }

  async function completeSession() {
    showSection("loading");
    document.querySelector(".lv1-loading-text").textContent = "結果を保存しています...";

    const allPassed = session.grades.every((g) => g.passed);

    try {
      ApiClient.hideError();
      await ApiClient.complete({
        session_id: session.session_id,
        questions: session.questions,
        answers: session.answers,
        grades: session.grades,
        final_passed: allPassed,
      });
    } catch (err) {
      ApiClient.showError(
        "結果の保存に失敗しました。リトライボタンで再試行できます。",
        () => completeSession()
      );
    }

    renderFinal(session);
  }

  // --- イベントバインド ---

  function bindEvents() {
    document.getElementById("btn-submit").addEventListener("click", submitAnswer);
    document.getElementById("btn-next").addEventListener("click", nextStep);
  }

  // --- 初期化 ---

  function init() {
    bindEvents();
    start();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  return { getSession, start };
})();
