// Package protocol 定义 Python 侧与 ntecalc 之间的 NDJSON 信封。
//
// 一行一个 JSON 对象，请求与响应靠 id 配对；stdout 只走协议，日志一律走 stderr
// （Python 侧会把 stderr 收进自己的日志，混进去的调试输出不会污染协议流）。
package protocol

import "encoding/json"

// Version 是协议版本。Python 侧启动时会用 --version 核对，不一致就整体回退。
const Version = 1

// OpLeagueRotation 是积分制动态轮换赛程的搜索请求。
const OpLeagueRotation = "league.rotation"

// Request 是一条请求。
type Request struct {
	ID   int64           `json:"id"`
	Op   string          `json:"op"`
	Args json.RawMessage `json:"args"`
}

// Response 是一条响应。OK=false 时 Error 必填。
type Response struct {
	ID    int64   `json:"id"`
	OK    bool    `json:"ok"`
	Error string  `json:"error,omitempty"`
	Data  any     `json:"data,omitempty"`
	MS    float64 `json:"ms"`
}
