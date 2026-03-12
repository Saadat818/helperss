1. helper_scan/templates/admin_trainer_versions.html:212
Level Critical
Status Confirmed
Trace

code
JavaScript
211 document.getElementById('versionModalTitle').textContent = `Версия v${version}`;
212 document.getElementById('versionModalContent').innerHTML = html;
213 document.getElementById('versionModal').style.display = 'block';
DerCodeFix
Suggested Change: Escaped special characters in the html string to prevent XSS attacks.
Fixed Code

code
JavaScript
212 document.getElementById('versionModalContent').innerHTML = html.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
2. helper_scan/templates/trainer_scenarios.html:706
Level Critical
Status Confirmed
Trace

code
JavaScript
705 });
706 document.getElementById('messagesList').innerHTML = html;
707 } catch(e) {
DerCodeFix
Suggested Change: Escaped fb.message to prevent XSS.
Fixed Code

code
JavaScript
699 &lt;div class=&quot;msg-text&quot;&gt;${escapeHtml(fb.message)}&lt;/div&gt;
3. helper_scan/helper7.py:4839
Level Critical
Status Confirmed
Trace

code
Python
4838 flash('Шаги не найдены')
4839 return redirect(redirect_url)
4840
DerCodeFix
Suggested Change: Added validation to ensure the redirect URL starts with '/' to prevent open redirects.
Fixed Code

code
Python
4839 if redirect_url.startswith('/'):
4840     return redirect(redirect_url)
4841 else:
4842     abort(400, "Invalid redirect URL")
4. helper_scan/helper7.py:4844
Level Critical
Status Confirmed
Trace

code
Python
4843 flash('Шаг не найден')
4844 return redirect(redirect_url)
4845
DerCodeFix
Suggested Change: Use url_for to generate internal URLs instead of using potentially untrusted strings.
Fixed Code

code
Python
4835 redirect_url = url_for('admin_edit_simple_manual', manual_id=manual_id)
5. helper_scan/helper7.py:3109#3111
Level Critical
Status Confirmed
Trace

code
Python
3109 return render_template('admin_trainer_versions.html',
3110     scenario=scenario,
3111     version_history=version_history)
DerCodeFix
Suggested Change: Escaped version_history to prevent XSS attack.
Fixed Code

code
Python
3111 version_history=escape(version_history))
6. helper_scan/helper7.py:3673
Level Critical
Status Confirmed
Trace

code
Python
3672
3673 return render_template('admin_trainer_audit.html', logs=logs, stats=stats, page=page)
3674
DerCodeFix
Suggested Change: Escaped the logs variable to prevent XSS attacks.
Fixed Code

code
Python
3673 return render_template('admin_trainer_audit.html', logs=escape(logs), stats=stats, page=page)
7. helper_scan/templates/trainer_play.html:958
Level Medium
Status Confirmed
Trace

code
JavaScript
957 } catch (error) {
958 console.error('Fetch error:', error);
959 }
DerCodeFix
Suggested Change: Removed detailed error message to prevent information leakage.
Fixed Code

code
JavaScript
958 console.error('Fetch error');
8. helper_scan/templates/trainer_play.html:1048
Level Medium
Status Confirmed
Trace

code
JavaScript
1047 } catch (e) {
1048 console.error(e);
1049 addBubble(answer.answer_text, 'operator');
DerCodeFix
Suggested Change: Changed the detailed error message to a generic one to prevent information leakage.
Fixed Code

code
JavaScript
1048 console.error('An error occurred');
9. helper_scan/templates/trainer_play.html:1281
Level Medium
Status Confirmed
Trace

code
JavaScript
1280 } catch (e) {
1281 console.error('Topic search error:', e);
1282 }
DerCodeFix
Suggested Change: Changed the error message to a generic one to prevent detailed information leak.
Fixed Code

code
JavaScript
1281 console.error('An error occurred during topic search.');
10. helper_scan/templates/admin_trainer.html:505
Level Medium
Status Confirmed
Trace

code
JavaScript
504
505 window.location.href = url;
506 }
DerCodeFix
Suggested Change: Added a function to validate the URL before redirection to prevent phishing attacks.
Fixed Code

code
JavaScript
497 function isAllowedRedirect(url) {
498     const allowedDomains = ['https://example.com'];
499     return allowedDomains.some(domain => url.startsWith(domain));
500 }
11. helper_scan/templates/admin_trainer_import.html:563
Level Medium
Status Confirmed
Trace

code
JavaScript
562 if (result.success) {
563 window.location.href = result.redirect_url || '{{ url_for("admin_trainer") }}';
564 }
DerCodeFix
Suggested Change: Added validation to check if the redirect URL is in the allowed list before performing the redirection.
Fixed Code

code
JavaScript
563 const allowedRedirects = ['https://example.com'];
564 const redirectUrl = result.redirect_url || '{{ url_for("admin_trainer") }}';
565 if (allowedRedirects.some(allowedUrl => redirectUrl.startsWith(allowedUrl))) {
566     window.location.href = redirectUrl;
567 } else {
568     window.location.href = '{{ url_for("admin_trainer") }}';
569 }