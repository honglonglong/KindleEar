#!/usr/bin/env python3
# -*- coding:utf-8 -*-
#Author: cdhigh <https://github.com/cdhigh>
#将发到string@appid.appspotmail.com的邮件正文转成附件发往kindle邮箱。

import re, zlib, base64, io
from urllib.parse import urljoin
from email.header import decode_header
from email.utils import parseaddr, collapse_rfc2231_value
from bs4 import BeautifulSoup
from flask import Blueprint, request
from apps.back_end.task_queue_adpt import create_http_task
from apps.back_end.db_models import KeUser, Book, WhiteList
from apps.base_handler import *
from apps.utils import local_time
from apps.back_end.send_mail_adpt import send_to_kindle
from config import *

from google.appengine.api import mail

bpInBoundEmail = Blueprint('bpInBoundEmail', __name__)

#subject of email will be truncated based limit of word count
SUBJECT_WORDCNT_FOR_APMAIL = 30

#if word count more than the number, the email received by appspotmail will 
#be transfered to kindle directly, otherwise, will fetch the webpage for links in email.
WORDCNT_THRESHOLD_FOR_APMAIL = 100

#clean css in dealing with content from string@appid.appspotmail.com or not
DELETE_CSS_FOR_APPSPOTMAIL = True

#解码邮件主题
def decode_subject(subject):
    if subject.startswith('=?') and subject.endswith('?='):
        subject = ''.join(str(s, c or 'us-ascii') for s, c in decode_header(subject))
    else:
        subject = str(collapse_rfc2231_value(subject))
    return subject

#判断一个字符串是否是超链接，返回链接本身，否则空串
def IsHyperLink(txt):
    expr = r"""^(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'".,<>???“”‘’]))"""
    match = re.match(expr, txt)
    return m.group() if match else ''

#从接收地址提取账号名和真实地址
#如果有多个收件人的话，只解释第一个收件人
def extractUsernameFromEmail(to):
    to = parseaddr(to)[1]
    to = to.split('@')[0] if to and '@' in to else 'xxx'
    if '__' in to:
        userNameParts = to.split('__')
        userName = userNameParts[0] if userNameParts[0] else ADMIN_NAME
        return userName, userNameParts[1]
    else:
        return ADMIN_NAME, to

#判断是否是垃圾邮件
#sender: 发件人地址
#user: 用户账号数据库行实例
def IsSpamMail(sender, user):
    if not sender or '@' not in sender:
        return True

    mailHost = sender.split('@')[1]
    whitelist = [item.mail.lower() for item in user.white_lists]

    return not (('*' in whitelist) or (sender.lower() in whitelist) or (f'@{mail_host}' in whitelist))

#GAE的退信通知
@bpInBoundEmail.post("/_ah/bounce")
def ReceiveBounce():
    msg = mail.BounceNotification(dict(request.form.lists()))
    default_log.warning("Bounce original: {}, notification: {}".format(msg.original, msg.notification))
    return "OK", 200

#有新的邮件到达
@bpInBoundEmail.post("/_ah/mail/<path>")
def ReceiveMail(path):
    global default_log
    log = default_log

    message = mail.InboundEmailMessage(request.get_data())

    #从接收地址提取账号名和真实地址
    userName, to = extractUsernameFromEmail(message.to)
    
    user = KeUser.get_one(KeUser.name == userName)
    if not user:
        userName = ADMIN_NAME
        user = KeUser.get_one(KeUser.name == userName)
    
    if not user or not user.kindle_email:
        return "OK", 200

    #阻挡垃圾邮件
    sender = parseaddr(message.sender)[1]
    if IsSpamMail(sender, user):
        self.response.out.write("Spam mail!")
        log.warning('Spam mail from : {}'.format(sender))
        return "OK", 200
    
    if hasattr(message, 'subject'):
        subject = decode_subject(message.subject).strip()
    else:
        subject = "NoSubject"
    
    forceToLinks = False
    forceToArticle = False

    #邮件主题中如果在最后添加一个 !links，则强制提取邮件中的链接然后生成电子书
    if subject.endswith('!links') or ' !links ' in subject:
        subject = subject.replace('!links', '').replace(' !links ', '').strip()
        forceToLinks = True
    # 如果邮件主题在最后添加一个 !article，则强制转换邮件内容为电子书，忽略其中的链接
    elif subject.endswith('!article') or ' !article ' in subject:
        subject = subject.replace('!article', '').replace(' !article ', '').strip()
        forceToArticle = True
        
    #通过邮件触发一次“现在投递”
    if to.lower() == 'trigger':
        TrigDeliver(subject, userName)
        return "OK", 200
    
    #获取和解码邮件内容
    txtBodies = message.bodies('text/plain')
    try:
        allBodies = [body.decode() for cType, body in message.bodies('text/html')]
    except:
        log.warning('Decode html bodies of mail failed.')
        allBodies = []
    
    #此邮件为纯文本邮件，将文本信息转换为HTML格式
    if not allBodies:
        log.info('There is no html body, use text body.')
        try:
            allBodies = [body.decode() for cType, body in txtBodies]
        except:
            log.warning('Decode text bodies of mail failed.')
            allBodies = []
        bodies = ''.join(allBodies)
        if not bodies:
            return "OK", 200

        bodyUrls = []
        for line in bodies.split('\n'):
            line = line.strip()
            if not line:
                continue
            link = IsHyperLink(line)
            if link:
                bodyUrls.append('<a href="{}">{}</a><br />'.format(link, link))
            else: #有非链接行则终止，因为一般的邮件最后都有推广链接
                break

        bodies = """<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
          <title>{}</title></head><body>{}</body></html>""".format(subject,
          ''.join(bodyUrls) if bodyUrls else bodies)
        allBodies = [bodies]
    
    #开始处理邮件内容
    soup = BeautifulSoup(allBodies[0], 'lxml')
    
    #合并多个邮件文本段
    for otherBody in allBodies[1:]:
        bodyOther = BeautifulSoup(otherBody, 'lxml').find('body')
        soup.body.extend(bodyOther.contents if bodyOther else [])
    
    #判断邮件内容是文本还是链接（包括多个链接的情况）
    links = []
    body = soup.body if soup.find('body') else soup
    if not forceToArticle: #如果强制转正文就不分析链接了，否则先分析和提取链接
        for s in body.stripped_strings:
            link = IsHyperLink(s)
            if link:
                if link not in links:
                    links.append(link)
            #如果是多个链接，则必须一行一个，不能留空，除非强制提取链接
            #这个处理是为了去除部分邮件客户端在邮件末尾添加的一个广告链接
            elif not forceToLinks:
                break
            
    if not links and not forceToArticle: #如果通过正常字符（显示出来的）判断没有链接，则看html的a标签
        links = [link['href'] for link in soup.find_all('a', attrs={'href': True})]
        
        text = ' '.join([s for s in body.stripped_strings])
        
        #如果有相对路径，则在里面找一个绝对路径，然后转换其他
        hasRelativePath = False
        fullPath = ''
        for link in links:
            text = text.replace(link, '')
            if not link.startswith('http'):
                hasRelativePath = True
            if not fullPath and link.startswith('http'):
                fullPath = link
        
        if hasRelativePath and fullPath:
            for idx, link in enumerate(links):
                if not link.startswith('http'):
                    links[idx] = urljoin(fullPath, link)
        
        #如果字数太多，则认为直接推送正文内容
        if not forceToLinks and (len(links) != 1 or len(text) > WORDCNT_THRESHOLD_FOR_APMAIL):
            links = []
        
    if links:
        #判断是下载文件还是转发内容
        isBook = bool(to.lower() in ('book', 'file', 'download'))
        if not isBook:
            isBook = bool(link[-5:].lower() in ('.mobi','.epub','.docx'))
        if not isBook:
            isBook = bool(link[-4:].lower() in ('.pdf','.txt','.doc','.rtf'))
        isDebug = bool(to.lower() == 'debug')

        if isDebug:
            bookType = 'Debug'
        elif isBook:
            bookType = 'Download'
        else:
            bookType = user.book_type
        
        #url需要压缩，避免URL太长
        params = {'u': userName,
                 'urls': base64.urlsafe_b64encode(zlib.compress('|'.join(links).encode('utf-8'))).decode(),
                 'type': bookType,
                 'to': user.kindle_email,
                 'tz': user.timezone,
                 'subj': subject[:SUBJECT_WORDCNT_FOR_APMAIL],
                 'lng': user.my_rss_book.language,
                 'kimg': '1' if user.my_rss_book.keep_image else '0'}
        create_http_task('/url2book', params)
    else: #直接转发邮件正文
        from lib.makeoeb import ImageMimeFromName
        imageContents = []
        if hasattr(message, 'attachments'):  #先判断是否有图片
            imageContents = [(f, c) for f, c in message.attachments if ImageMimeFromName(f)]
        
        #先修正不规范的HTML邮件
        h = soup.find('head')
        if not h:
            h = soup.new_tag('head')
            soup.html.insert(0, h)
        t = soup.head.find('title')
        if not t:
            t = soup.new_tag('title')
            t.string = subject
            soup.head.insert(0, t)
        
        #有图片的话，要生成MOBI或EPUB才行
        #而且多看邮箱不支持html推送，也先转换epub再推送
        if imageContents or (user.book_type == "epub"):
            from lib.makeoeb import (GetOpts, CreateEmptyOeb, setMetaData, EPUBOutput, MOBIOutput)
            
            #仿照Amazon的转换服务器的处理，去掉CSS
            if DELETE_CSS_FOR_APPSPOTMAIL:
                tag = soup.find('style', attrs={'type': 'text/css'})
                if tag:
                    tag.decompose()
                for tag in soup.find_all(attrs={'style': True}):
                    del tag['style']
            
            #将图片的src的文件名调整好
            for img in soup.find_all('img', attrs={'src': True}):
                if img['src'].lower().startswith('cid:'):
                    img['src'] = img['src'][4:]
            
            opts = GetOpts()
            oeb = CreateEmptyOeb(opts, log)
            
            setMetaData(oeb, subject[:SUBJECT_WORDCNT_FOR_APMAIL], user.my_rss_book.language, 
                local_time(tz=user.timezone), pubtype='book:book:KindleEar')

            id_, href = oeb.manifest.generate(id='page', href='page.html')
            manif = oeb.manifest.add(id_, href, 'application/xhtml+xml', data=str(soup))
            oeb.spine.add(manif, False)
            oeb.toc.add(subject, href)
            
            for fileName, content in imageContents:
                try:
                    content = content.decode()
                except:
                    pass
                else:
                    id_, href = oeb.manifest.generate(id='img', href=fileName)
                    oeb.manifest.add(id_, href, ImageMimeFromName(fileName), data=content)
            
            oIO = io.BytesIO()
            o = EPUBOutput() if user.book_type == "epub" else MOBIOutput()
            o.convert(oeb, oIO, opts, log)
            send_to_kindle(userName, user.kindle_email, subject[:SUBJECT_WORDCNT_FOR_APMAIL], 
                user.book_type, oIO.getvalue(), user.timezone)
        else: #没有图片则直接推送HTML文件，阅读体验更佳
            m = soup.find('meta', attrs={"http-equiv": "Content-Type"})
            if not m:
                m = soup.new_tag('meta', content="text/html; charset=utf-8")
                m["http-equiv"] = "Content-Type"
                soup.html.head.insert(0, m)
            else:
                m['content'] = "text/html; charset=utf-8"
            
            send_to_kindle(userName, user.kindle_email, subject[:SUBJECT_WORDCNT_FOR_APMAIL],
                'html', str(soup), user.timezone, False)
    
    return "OK", 200

#触发一次推送 
#邮件主题为需要投递的书籍，为空或all则等同于网页的"现在投递"按钮
#如果是书籍名，则单独投递，多个书籍名使用逗号分隔
def TrigDeliver(subject, userName):
    if subject.lower() in ('NoSubject', 'all'):
        create_http_task('/deliver', {'u': userName})
    else:
        bkIds = []
        for bk in subject.split(','):
            trigBook = Book.get_one(Book.title == bk.strip())
            if trigBook:
                bkIds.append(trigBook.key_or_id_string)
            else:
                log.warning('Book not found : {}'.format(bk.strip()))
        if bkIds:
            create_http_task('/worker', {'u': userName, 'id_': ','.join(bkIds)})