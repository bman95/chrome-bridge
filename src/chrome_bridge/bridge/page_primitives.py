"""Pure JavaScript builders for DOM-facing page primitives.

The client owns transport and tab lifecycle; this module owns escaping and the
exact JavaScript evaluated in the page.
"""

from __future__ import annotations

import json


def js_string(value: str) -> str:
    return json.dumps(value)


def selector_present(selector: str) -> str:
    return f"document.querySelector({js_string(selector)})!==null"


def outer_html(container: str) -> str:
    selector = js_string(container)
    return (
        f"(function(){{var e=document.querySelector({selector});"
        "return e?e.outerHTML:document.documentElement.outerHTML;}})()"
    )


def _center_helper() -> str:
    return (
        "var __cbCenter=function(el){"
        "var r=el.getBoundingClientRect();"
        "if(r.width===0||r.height===0)return null;"
        "var s=window.getComputedStyle(el);"
        "if(s.visibility==='hidden'||s.display==='none')return null;"
        "return {x:r.left+r.width/2,y:r.top+r.height/2};};"
    )


def text_center(text: str, *, tag: str | None, contains: bool = False) -> str:
    wanted = js_string(text)
    tag_filter = js_string(tag.lower()) if tag else "null"
    match_fn = (
        "function match(s){return norm(s).toLowerCase().indexOf(target)!==-1;}"
        if contains
        else "function match(s){return norm(s)===target;}"
    )
    target_expr = "norm(wanted).toLowerCase()" if contains else "norm(wanted)"
    expression = (
        "(function(){"
        f"var wanted={wanted};var tag={tag_filter};"
        "function norm(s){return (s||'').replace(/\\s+/g,' ').trim();}"
        f"{match_fn}"
        f"var target={target_expr};"
        "var els=document.querySelectorAll(tag?tag:'*');"
        "var best=null;"
        "for(var i=0;i<els.length;i++){var el=els[i];"
        "if(!match(el.textContent))continue;"
        "var inner=el.querySelectorAll(tag?tag:'*');var wrapper=false;"
        "for(var j=0;j<inner.length;j++){"
        "if(match(inner[j].textContent)){wrapper=true;break;}}"
        "if(wrapper)continue;"
        "best=el;break;}"
        "if(!best)return null;return __cbCenter(best);})()"
    )
    return _center_helper() + expression


def selector_center(selector: str) -> str:
    encoded = js_string(selector)
    expression = (
        "(function(){"
        f"var el=document.querySelector({encoded});"
        "if(!el)return null;return __cbCenter(el);})()"
    )
    return _center_helper() + expression


def query_text(selector: str) -> str:
    encoded = js_string(selector)
    return (
        "(function(){"
        f"var el=document.querySelector({encoded});"
        "if(!el)return null;"
        "return (el.innerText||el.textContent||'').trim();})()"
    )


def get_attribute(selector: str, name: str) -> str:
    encoded_selector = js_string(selector)
    encoded_name = js_string(name)
    return (
        "(function(){"
        f"var el=document.querySelector({encoded_selector});"
        "if(!el)return null;"
        f"return el.getAttribute({encoded_name});}})()"
    )


def input_value(selector: str) -> str:
    encoded = js_string(selector)
    return (
        "(function(){"
        f"var el=document.querySelector({encoded});"
        "if(!el)return null;"
        "return el.value==null?'':String(el.value);})()"
    )


def fill(selector: str, text: str) -> str:
    encoded_selector = js_string(selector)
    encoded_value = js_string(text)
    return (
        "(function(){"
        f"var el=document.querySelector({encoded_selector});"
        "if(!el)return false;"
        "el.focus();"
        f"el.value={encoded_value};"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return true;})()"
    )


def banner_present(selector: str) -> str:
    return f"!!document.querySelector({js_string(selector)})"
