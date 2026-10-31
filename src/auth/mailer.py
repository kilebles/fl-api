"""Transactional authentication emails delivered via the Resend HTTP API."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import html
import httpx
from loguru import logger

RESEND_API_URL = "https://api.resend.com/emails"


class MailDeliveryError(RuntimeError):
    """Raised when Resend rejects or fails to deliver an email."""


@dataclass(slots=True)
class EmailMessage:
    to: str
    subject: str
    html: str
    text: str


EmailSender = Callable[[EmailMessage], Awaitable[None]]


def _wrap_html(title: str, body_html: str, footer_note: str) -> str:
    safe_title = html.escape(title)
    safe_footer = html.escape(footer_note)
    return (
        '<!doctype html><html lang="ru"><body style="margin:0;padding:32px 16px;'
        "background:#FAF9F5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        'color:#141413;">'
        '<div style="max-width:460px;margin:0 auto;background:#FFFFFF;border:1px solid #E8E5DC;'
        'border-radius:14px;padding:28px 28px 24px;">'
        '<div style="font-size:13px;font-weight:600;letter-spacing:0.02em;color:#73726C;margin-bottom:18px;">'
        "Motion Pr</div>"
        f'<h1 style="margin:0 0 14px;font-size:20px;line-height:1.3;font-weight:600;color:#141413;">{safe_title}</h1>'
        f"{body_html}"
        '<hr style="border:none;border-top:1px solid #EFECE4;margin:24px 0 14px;" />'
        f'<p style="margin:0;font-size:12px;line-height:1.5;color:#8C8A82;">{safe_footer}</p>'
        "</div></body></html>"
    )


class ResendMailer:
    """Async Resend email client with a pluggable sender for deterministic tests."""

    def __init__(
        self,
        api_key: str = "",
        from_address: str = "",
        sender: EmailSender | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._from = from_address.strip()
        self._sender = sender

    @property
    def enabled(self) -> bool:
        return self._sender is not None or bool(self._api_key and self._from)

    async def send(self, message: EmailMessage) -> None:
        if self._sender is not None:
            await self._sender(message)
            return
        if not self.enabled:
            raise MailDeliveryError("Отправка писем не настроена на сервере (RESEND_API_KEY)")

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    RESEND_API_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": self._from,
                        "to": [message.to],
                        "subject": message.subject,
                        "html": message.html,
                        "text": message.text,
                    },
                )
            except httpx.HTTPError as exc:
                logger.error(f"[mail] Resend network error for {message.to}: {exc}")
                raise MailDeliveryError("Не удалось связаться с почтовым сервисом. Попробуйте позже.") from exc

        if resp.status_code >= 400:
            logger.error(f"[mail] Resend HTTP {resp.status_code} for {message.to}: {resp.text[:300]}")
            raise MailDeliveryError("Не удалось отправить письмо. Проверьте адрес почты или попробуйте позже.")

    async def send_password_reset(self, to: str, reset_url: str) -> None:
        safe_url = html.escape(reset_url, quote=True)
        body = (
            '<p style="margin:0 0 20px;font-size:14px;line-height:1.55;color:#3D3D3A;">'
            "Мы получили запрос на смену пароля вашего аккаунта. Нажмите кнопку ниже, чтобы задать новый пароль "
            "(ссылка действует 30 минут):"
            "</p>"
            f'<p style="margin:0 0 18px;"><a href="{safe_url}" '
            'style="display:inline-block;background:#B85A3A;color:#FFFFFF;text-decoration:none;'
            'font-size:14px;font-weight:500;padding:11px 20px;border-radius:8px;">Сбросить пароль</a></p>'
            '<p style="margin:0;font-size:12px;line-height:1.5;color:#73726C;word-break:break-all;">'
            f"Или откройте ссылку в браузере:<br />{safe_url}</p>"
        )
        await self.send(
            EmailMessage(
                to=to,
                subject="Сброс пароля в Motion Pr",
                html=_wrap_html(
                    "Сброс пароля",
                    body,
                    "Если вы не запрашивали сброс пароля, ничего делать не нужно — ваш текущий пароль останется прежним.",
                ),
                text=(
                    "Для сброса пароля в Motion Pr откройте ссылку (действует 30 минут):\n"
                    f"{reset_url}\n\n"
                    "Если вы не запрашивали сброс пароля, проигнорируйте это письмо."
                ),
            )
        )

    async def send_email_verification(self, to: str, verify_url: str, code: str) -> None:
        safe_url = html.escape(verify_url, quote=True)
        safe_code = html.escape(code)
        body = (
            '<p style="margin:0 0 16px;font-size:14px;line-height:1.55;color:#3D3D3A;">'
            "Введите этот 6-значный код на странице регистрации/входа или нажмите кнопку ниже, чтобы активировать аккаунт:"
            "</p>"
            '<div style="background:#FAF9F5;border:1px solid #E5E1D6;border-radius:10px;'
            'padding:14px;margin:0 0 18px;text-align:center;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
            f'font-size:26px;font-weight:700;letter-spacing:0.22em;color:#141413;">{safe_code}</div>'
            f'<p style="margin:0 0 16px;"><a href="{safe_url}" '
            'style="display:inline-block;background:#B85A3A;color:#FFFFFF;text-decoration:none;'
            'font-size:14px;font-weight:500;padding:11px 20px;border-radius:8px;">Подтвердить почту</a></p>'
            '<p style="margin:0;font-size:12px;line-height:1.5;color:#73726C;word-break:break-all;">'
            f"{safe_url}</p>"
        )
        await self.send(
            EmailMessage(
                to=to,
                subject=f"{code} — подтверждение почты в Motion Pr",
                html=_wrap_html(
                    "Подтвердите почту",
                    body,
                    "Код и ссылка действительны 24 часа. Если вы не регистрировались в Motion Pr, просто проигнорируйте это письмо.",
                ),
                text=(
                    f"Код подтверждения почты в Motion Pr: {code}\n\n"
                    f"Или подтвердите в один клик по ссылке:\n{verify_url}\n"
                ),
            )
        )

    async def send_email_change_code(self, to: str, code: str) -> None:
        safe_code = html.escape(code)
        body = (
            '<p style="margin:0 0 16px;font-size:14px;line-height:1.55;color:#3D3D3A;">'
            "Введите этот код в настройках аккаунта, чтобы подтвердить смену адреса электронной почты (код действует 15 минут):"
            "</p>"
            '<div style="background:#FAF9F5;border:1px solid #E5E1D6;border-radius:10px;'
            'padding:14px;text-align:center;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
            f'font-size:26px;font-weight:700;letter-spacing:0.22em;color:#141413;">{safe_code}</div>'
        )
        await self.send(
            EmailMessage(
                to=to,
                subject=f"{code} — подтверждение новой почты в Motion Pr",
                html=_wrap_html(
                    "Подтверждение новой почты",
                    body,
                    "Если вы не запрашивали смену почты в Motion Pr, проигнорируйте это письмо.",
                ),
                text=f"Код для подтверждения новой почты в Motion Pr: {code}\n",
            )
        )
