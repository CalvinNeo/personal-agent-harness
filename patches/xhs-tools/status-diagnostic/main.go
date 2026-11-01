package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/xpzouying/xiaohongshu-mcp/browser"
	"github.com/xpzouying/xiaohongshu-mcp/cookies"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
)

func main() {
	logrus.SetLevel(logrus.WarnLevel)
	report := map[string]any{"cookies_modified": false, "outbound_qq_messages": 0}
	started := time.Now()
	defer func() {
		if recover() != nil {
			report["error"] = "browser operation failed"
		}
		report["elapsed_ms"] = time.Since(started).Milliseconds()
		data, _ := json.Marshal(report)
		fmt.Println(string(data))
	}()
	store := cookies.NewLoadCookie(cookies.GetCookiesFilePath())
	instance := browser.NewBrowser(true, browser.WithFingerprintSeed(store.LoadSeed()))
	defer instance.Close()
	report["browser_ready_ms"] = time.Since(started).Milliseconds()
	page := instance.NewPage()
	defer page.Close()
	action := xiaohongshu.NewLogin(page)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	loggedIn, err := action.CheckLoginStatus(ctx)
	report["is_logged_in"] = loggedIn
	if err != nil {
		report["error"] = err.Error()
	}
	report["status_elapsed_ms"] = time.Since(started).Milliseconds()
	state, err := page.Timeout(3 * time.Second).Eval(`() => {
		const user = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
		const raw = user && user.userInfo;
		const info = raw && raw.value !== undefined ? raw.value : raw;
		const text = document.body ? document.body.innerText : "";
		return {ready: document.readyState,
			authenticatedMarker: !!document.querySelector('.main-container .user .link-wrapper .channel'),
			qrPresent: !!document.querySelector('.qrcode-img'),
			authenticatedUserState: !!(info && info.guest === false && (info.userId || info.user_id)),
			verification: /安全验证|访问频繁|访问受限|异常访问|验证码|验证身份/.test(text)};
	}`)
	if err == nil {
		report["safe_page_state"] = state.Value
	}
}
