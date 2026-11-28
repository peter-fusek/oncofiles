"""Slovak versions of /privacy and /terms (#1008, maintainer decision 2026-11-27).

The English pages live in ``server.py`` (``privacy_policy`` / ``terms_of_service``);
these bodies are their Slovak translations with the SAME sections, numbers and
anchor ids. Change both languages together: ``tests/test_1008_sk_legal.py``
compares the section ids and the numbers, and the claim tests check both pages.
``__CONTACT__`` is replaced by the server with the contact address.
"""

from __future__ import annotations

PRIVACY_TITLE = "Zásady ochrany osobných údajov"
TERMS_TITLE = "Podmienky používania"

PRIVACY_BODY = """
<p class="meta">Účinné od: 25. marca 2026 · Naposledy aktualizované: 28. novembra 2026 ·
Verzia súhlasu 2026-11-28</p>

<p>Oncofiles prevádzkuje spoločnosť <strong>Instarea s.r.o.</strong> („my“). Tieto zásady
opisujú, ako Oncofiles (<a href="https://oncofiles.com">oncofiles.com</a>) zaobchádza s vašimi
údajmi tak, ako služba funguje dnes.</p>

<h2 id="access">1. K akým údajom pristupujeme</h2>
<ul>
<li><strong>Prihlásenie cez Google</strong> — e-mailová adresa vášho účtu Google. Podľa nej
rozhodujeme, ktorých pacientov môžete vidieť.</li>
<li><strong>Google Drive</strong> — obrazovka súhlasu Google žiada prístup k celému vášmu Google
Drive (čítanie aj zápis). Užšie oprávnenie Google by pokrylo len súbory, ktoré vytvoril sám
Oncofiles, takže Oncofiles by nevidel súbor, ktorý do priečinka pridáte sami, napríklad sken
z telefónu. Oncofiles číta a mení <strong>len priečinok, ktorý pre pacienta vyberiete</strong>.
V tomto priečinku môže súbory premenovať na štandardný názov, presunúť ich do podpriečinkov
(napríklad Labs alebo Reports), pridať ku každému súboru textový prepis (<code>.md</code>) a viesť
súbor <code>README.md</code> s pravidlami priečinka a niekoľko súborov, ktoré zaznamenávajú jeho
vlastnú prácu. <strong>Oncofiles nikdy nič natrvalo nemaže.</strong> Automatické spracovanie
nepresúva vaše súbory do koša.</li>
<li><strong>Gmail a Google Kalendár</strong> — momentálne sa nepoužívajú. Ak ich neskôr pridáme,
najprv vás požiadame o samostatný súhlas.</li>
</ul>
<p>Oncofiles ešte nedokončil overenie aplikácie v Google. Google preto pred obrazovkou súhlasu
zobrazí upozornenie „Google túto aplikáciu neoveril“ a povolí najviac 100 používateľov.
Upozornenie znamená, že Google aplikáciu zatiaľ neposúdil.</p>
<p>Pristupujeme len k údajom, na ktoré nám výslovne dáte oprávnenie na obrazovke súhlasu Google
(OAuth). Prístup môžete kedykoľvek odvolať v
<a href="https://myaccount.google.com/permissions">oprávneniach účtu Google</a>.</p>

<h2 id="consent">2. Kto môže Oncofiles používať a súhlas</h2>
<ul>
<li>Noví používatelia si zatiaľ nemôžu pripojiť vlastný Google Drive. Keď sa to otvorí, najprv
len na pozvánku.</li>
<li>Pacientom môže byť človek s rakovinou alebo človek, ktorý vedie bežné zdravotné
záznamy.</li>
<li>Pacienta si môžete vytvoriť pre seba alebo ako opatrovateľ pre inú osobu. Pri vytvorení
pacienta potvrdzujete, že ste touto osobou, alebo že máte jej súhlas či iné oprávnenie (napríklad
ako rodič alebo zákonný zástupca) spravovať jej zdravotné údaje, a súhlasíte so spracúvaním
týchto zdravotných údajov tak, ako je tu opísané. Zdravotné údaje sú osobitnou kategóriou
osobných údajov a spracúvame ich na základe tohto výslovného súhlasu
(čl. 9 ods. 2 písm. a) GDPR).</li>
<li>Toto potvrdenie zaznamenávame: verziu tohto textu, čas a kľúčovaný odtlačok (hash) vašej
e-mailovej adresy. Záznam samotnú e-mailovú adresu neobsahuje.</li>
</ul>

<h2 id="processing">3. Ako spracúvame vaše údaje</h2>
<ul>
<li>Rozpoznávanie textu (OCR), triedenie dokumentov a vyťahovanie hodnôt bežia
<strong>na lokálnom AI modeli na našom vlastnom serveri na Slovensku (EÚ)</strong>. Spracovanie
na pozadí neposiela vaše dokumenty žiadnej cloudovej AI službe. Možnosť cloudovej AI je na
serveri vypnutá a indexovanie aj vyťahovanie hodnôt odmietajú volania cloudovej AI priamo vo
svojom kóde, aj keby bola zapnutá.</li>
<li>Laboratórne hodnoty sa z rozpoznaného textu vyťahujú pravidlami. Diagnózy, liečbu a podobné
údaje číta lokálny model a server overí, že ich text je na strane vytlačený. Každá hodnota si
drží odkaz na zdrojový dokument, stranu a text a stav overenia: overené strojom, prečítané
modelom s textom overeným na strane, neoverené, alebo overené či opravené človekom.</li>
<li><strong>Keď sa na svoje záznamy pýtate vo vlastnom AI asistentovi</strong> (napríklad
Claude alebo ChatGPT pripojenom k Oncofiles), asistent dostane od Oncofiles to, čo si pre vašu
otázku vyžiada (text dokumentov, hodnoty, zhrnutia), ako súčasť vašej konverzácie. Deje sa to
len vtedy, keď sa pýtate, a riadi sa to vašou vlastnou zmluvou s poskytovateľom tohto
asistenta vrátane jeho nastavení uchovávania a trénovania.</li>
</ul>
<h3 id="by-email">Dokumenty poslané e-mailom</h3>
<p>Každý pacient môže mať súkromnú e-mailovú adresu pre dokumenty. Kto pošle dokumenty na túto
adresu, súhlasí s <a href="/sk/terms">podmienkami používania</a> a so spracovaním týchto
dokumentov podľa týchto zásad; odosielateľ dostane e-mail, ktorý to uvádza, s odkazom na
podmienky. Do priečinka pacienta sa pridajú len prílohy poslané z adresy opatrovateľa pacienta
(s odosielateľom potvrdeným poštovým systémom) a spracujú sa ako každý iný dokument. Text e-mailu
opatrovateľa sa neukladá a žiadny text e-mailu nečíta AI model. Dokumenty, ktoré na adresu
pacienta pošle ktokoľvek iný než opatrovateľ, čakajú zašifrované v karanténe, kým ich
opatrovateľ neschváli alebo neodmietne, najviac 7 dní; odmietnuté položky a položky nerozhodnuté
ani po 7 dňoch zostávajú 30 dní v koši a potom sa vymažú (schválené sa zaradia ako zvyčajne)
a odosielateľ nedostane žiadnu odpoveď. Aby sa opatrovateľ mohol rozhodnúť, čistý text takého
e-mailu sa uchováva spolu s nimi, zašifrovaný, rovnako dlho a vymaže sa spolu so súbormi;
v dashboarde opatrovateľa sa zobrazí malý náhľad každej prílohy. Dokumenty odstránené po
nevyužitej skúšobnej dobe sa ešte najviac 6 mesiacov dajú obnoviť zo šifrovaných záloh.</p>
<h3 id="chat">Chat s vašimi záznamami</h3>
<p>Ak používate chat zabudovaný v Oncofiles, vaše otázky a jeho odpovede sa ukladajú na našom
serveri, zašifrované zvlášť pre každého pacienta, aby sa v konverzácii dalo pokračovať. Spracúva
ich len lokálny AI model na našom vlastnom serveri, nikdy cloudový AI model. Po 90 dňoch sa
konverzácia presunie do koša, kde sa ešte dá obnoviť, a o 30 dní neskôr sa jej text natrvalo
vymaže. História chatu je súčasťou exportu údajov pacienta a vymaže sa spolu s pacientom.</p>
<p>Vaše údaje <strong>nepoužívame</strong> na reklamu, profilovanie ani na žiadny účel, ktorý
nesúvisí so správou vašich zdravotných dokumentov.</p>

<h2 id="storage">4. Kde sú údaje uložené</h2>
<ul>
<li><strong>Na našom serveri na Slovensku (EÚ), na šifrovanom disku</strong> — profil pacienta
(meno, dátum narodenia, typ pacienta, voliteľná poznámka o diagnóze a e-mailová adresa
opatrovateľa), prístupový token Google pre Drive, index dokumentov, rozpoznaný text, vyťažené
hodnoty so stavom overenia, zašifrovaná história chatu (časť 3), záznam o súhlase (časť 2)
a záznam prístupov (časť 6).</li>
<li><strong>Zálohy</strong> — lokálne a šifrované, uchovávané obmedzený čas. Pred hromadnou
úpravou vyťažených hodnôt a pred vymazaním pacienta na žiadosť server urobí aj bezpečnostnú
kópiu databázy; uchováva sa len 5 najnovších bezpečnostných kópií.</li>
<li><strong>Logy</strong> — technický log aplikácie maskuje e-mailové adresy a tokeny
a uchováva sa 7 dní. Server vidí IP adresy požiadaviek, kvôli bezpečnosti a obmedzeniu
zneužívania. Serverové logy sa automaticky rotujú a uchovávajú najviac 30 dní.</li>
<li><strong>Váš Google Drive</strong> — vaše pôvodné súbory a k nim prepisy a
<code>README.md</code> opísané v časti 1.</li>
</ul>

<h2 id="sharing">5. Zdieľanie údajov</h2>
<p>Vaše osobné údaje <strong>nepredávame, nezdieľame ani neprenášame</strong>. Ďalšie zapojené
služby:</p>
<ul>
<li><strong>Google</strong> — ukladá vaše súbory vo vašom vlastnom účte Google Drive.</li>
<li><strong>Váš AI asistent</strong> (napríklad Claude od Anthropic alebo ChatGPT od OpenAI) —
len to, čo si vyžiada, keď sa pýtate (časť 3).</li>
<li><strong>Náš poskytovateľ e-mailu</strong> (doručovanie e-mailov, EÚ) — ak sa zapíšete do
poradovníka, vaša e-mailová adresa sa použije na upozornenie pre nás a na to, aby sme vám
napísali o prístupe. Pri vytvorení pacienta dostaneme upozornenie so skráteným identifikátorom
pacienta a e-mailovou adresou opatrovateľa, nikdy nie s menom pacienta. Neposielajú sa žiadne
zdravotné dokumenty ani hodnoty. Poskytovateľ môže do našich e-mailov pridať sledovací pixel
a presmerovať ich odkazy cez vlastné servery, aby počítal otvorenia a kliknutia; vlastné
sledovanie nepridávame. Ak sa chcete zo zoznamu odhlásiť, odpovedzte na ktorýkoľvek náš
e-mail.</li>
<li><strong>Google Analytics</strong> — len na našich verejných stránkach (úvodná stránka
a demo) a len potom, čo v lište cookies prijmete analytické cookies. Meria návštevy (zobrazené
stránky, približnú polohu, zariadenie, odkiaľ ste prišli). Aplikácia po prihlásení Google
Analytics nenačítava a nikdy nedostane vaše zdravotné dokumenty ani hodnoty.</li>
</ul>
<p>Hostingom je náš vlastný server na Slovensku (EÚ); vaše údaje neukladá ani nespracúva žiadny
externý poskytovateľ hostingu ani databázy.</p>

<h2 id="audit">6. Záznam prístupov</h2>
<p>Každá požiadavka, ktorú AI asistent pošle do Oncofiles, sa zaznamená: použitý nástroj, čas,
pacient, spôsob prihlásenia volajúceho, či bola úspešná, a kľúčovaný odtlačok (hash) e-mailovej
adresy volajúceho (nikdy nie samotná adresa). Argumenty sa zaznamenávajú len vtedy, keď nemôžu
obsahovať zdravotné údaje (napríklad číslo dokumentu alebo voľba); všetko ostatné sa ukladá len
ako dĺžka. Tento záznam používame na to, aby sme vedeli odpovedať, kto pristupoval ku ktorým
záznamom.</p>
<p>Správcovia služby môžu otvoriť záznamy ktoréhokoľvek pacienta, kvôli prevádzke
a podpore.</p>
<p>Webový dashboard vedie vlastný záznam prístupov: každé prihlásenie, každý odmietnutý pokus
otvoriť pacienta a každé otvorenie pacienta správcom, ktorý nie je jeho opatrovateľom. Riadok
obsahuje čas, udalosť a kľúčovaný odtlačok (hash) e-mailovej adresy volajúceho (nikdy nie
samotnú adresu). Tieto riadky sa automaticky vymažú po 90 dňoch.</p>

<h2 id="retention">7. Uchovávanie údajov</h2>
<p>Vaše údaje uchovávame, kým je váš účet aktívny. Oncofiles automaticky nemaže vaše dokumenty,
ich text, vyťažené hodnoty ani záznamy pacientov. Podľa plánu expirujú len technické záznamy,
napríklad log aplikácie po 7 dňoch, ostatné serverové logy najneskôr po 30 dňoch (časť 4)
a záznam prístupov do dashboardu po 90 dňoch (časť 6). História chatu sa po 90 dňoch presunie
do koša a jej text sa o 30 dní neskôr vymaže (časť 3). Môžete nás požiadať o vymazanie všetkých
údajov uložených o pacientovi vrátane vyťažených hodnôt a záznamu prístupov; pozri časť 8.
Pôvodné súbory vo vašom Google Drive Oncofiles nikdy nemaže.</p>

<h2 id="rights">8. Vaše práva (GDPR)</h2>
<p>Ak ste v EÚ/EHP, máte právo:</p>
<ul>
<li>na prístup k svojim osobným údajom</li>
<li>požiadať o opravu alebo vymazanie</li>
<li>na prenosnosť údajov (export vašich údajov)</li>
<li>kedykoľvek odvolať súhlas</li>
<li>podať sťažnosť dozornému orgánu (na Slovensku: Úrad na ochranu osobných údajov Slovenskej
republiky)</li>
</ul>
<p>Opatrovateľ pacienta si v dashboarde môže stiahnuť export všetkého, čo Oncofiles o pacientovi
ukladá (ZIP so súbormi JSON; pôvodné súbory zostávajú vo vašom Google Drive), a môže odpojiť
Google Drive: Oncofiles potom vymaže uložený prístupový token Google, požiada Google o jeho
zrušenie, ak ten istý účet Google nepoužíva žiadny iný pacient, a prestane priečinok spracúvať;
súbory vo vašom Drive sa nezmenia. Prístup Oncofiles k vášmu Google Drive môžete kedykoľvek
zastaviť aj vo svojom účte Google (časť 1).</p>
<p>Ostatné práva uplatníte e-mailom (časť 10). Žiadosti o prístup, export a vymazanie
vybavujeme ručne, do 30 dní. Vymazanie odstráni všetko, čo je o pacientovi uložené v našej
databáze, vrátane vyťažených hodnôt, záznamu o súhlase (časť 2) a záznamu prístupov (časť 6);
súborov vo vašom Google Drive sa nikdy nedotkne. Pred vymazaním server urobí jednu bezpečnostnú
kópiu databázy (časť 4); vymazané údaje v nej môžu zostať, kým ju nenahradia novšie, a
v šifrovaných zálohách najviac 6 mesiacov.</p>

<h2 id="google-api">9. Informácie o používaní Google API</h2>
<p>Používanie a prenos informácií získaných z Google API v Oncofiles dodržiava
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API
Services User Data Policy</a> vrátane požiadaviek Limited Use.</p>

<h2 id="contact">10. Kontakt</h2>
<p>Otázky k ochrane osobných údajov: <a href="mailto:__CONTACT__">__CONTACT__</a></p>
<p>Instarea s.r.o. · Bratislava, Slovensko</p>
"""

TERMS_BODY = """
<p class="meta">Účinné od: 25. marca 2026 · Naposledy aktualizované: 27. novembra 2026 ·
Verzia súhlasu 2026-11-28</p>

<p>Tieto podmienky upravujú používanie služby Oncofiles
(<a href="https://oncofiles.com">oncofiles.com</a>), ktorú prevádzkuje spoločnosť
<strong>Instarea s.r.o.</strong> („my“).</p>

<h2 id="service">1. Opis služby</h2>
<p>Oncofiles udržiava priečinok zdravotných dokumentov v Google Drive usporiadaný. Rozpoznáva ich
text lokálnym AI modelom na vlastnom serveri na Slovensku, vyťahuje hodnoty s odkazom na ich
zdroj a sprístupňuje záznamy AI asistentom, ako sú Claude a ChatGPT, keď sa ich spýtate. Je
určený pre onkologických pacientov, ľudí, ktorí vedú bežné zdravotné záznamy, a ich
opatrovateľov.</p>

<h2 id="not-medical-advice">2. Nie je to lekárska rada</h2>
<p><strong>Oncofiles nie je zdravotnícka pomôcka a neposkytuje lekárske rady.</strong>
Usporadúva a zobrazuje vaše existujúce zdravotné dokumenty. Všetky medicínske rozhodnutia robte
so svojimi lekármi. Automaticky rozpoznaný text a vyťažené hodnoty môžu byť nesprávne; overte si
ich v zdrojovom dokumente, na ktorý každá hodnota odkazuje. Zhrnutia a analýzy vytvorené AI
slúžia len na informačné účely.</p>

<h2 id="your-data">3. Vaše údaje</h2>
<p>Vaše zdravotné dokumenty a údaje zostávajú úplne vaše. Oncofiles pristupuje k vašim službám
Google len s vaším výslovným súhlasom, ktorý môžete kedykoľvek odvolať. Podrobnosti nájdete
v <a href="/sk/privacy">zásadách ochrany osobných údajov</a>.</p>
<p>Pri vytvorení pacienta potvrdzujete, že ste touto osobou, alebo že máte jej súhlas či iné
oprávnenie (napríklad ako rodič alebo zákonný zástupca) spravovať jej zdravotné údaje
v Oncofiles. Nevytvárajte pacienta pre niekoho bez takého súhlasu alebo oprávnenia.</p>

<h3 id="by-email">Dokumenty poslané e-mailom</h3>
<p>Kto pošle dokumenty na e-mailovú adresu pacienta pre dokumenty, súhlasí s týmito podmienkami
a so spracovaním týchto dokumentov podľa <a href="/sk/privacy">zásad ochrany osobných
údajov</a>. Dokumenty sa čítajú hneď, bez osobitnej obrazovky so súhlasom; odosielateľ dostane
e-mail, ktorý to uvádza, s odkazom na tieto podmienky. Do priečinka pacienta sa pridajú len
prílohy z adresy opatrovateľa pacienta. Adresa umožňuje každému, kto ju pozná, dokumenty
pacientovi poslať, nikdy nie čítať: nezverejňujte ju, a ak sa dostane do nesprávnych rúk,
vytvorte si v dashboarde novú.</p>

<h2 id="account">4. Účet a prístup</h2>
<ul>
<li>Na používanie Oncofiles potrebujete účet Google</li>
<li>Noví používatelia si zatiaľ nemôžu pripojiť vlastný Google Drive. Keď sa to otvorí, najprv
len na pozvánku</li>
<li>Oncofiles ešte nedokončil overenie aplikácie v Google, preto Google zobrazuje upozornenie na
neoverenú aplikáciu a povolí najviac 100 používateľov</li>
<li>Zodpovedáte za bezpečnosť svojich prístupových tokenov (bearer) a prihlasovacích
údajov</li>
<li>Bezplatná úroveň: najviac 500 dokumentov na pacienta</li>
</ul>

<h2 id="acceptable-use">5. Prijateľné používanie</h2>
<p>Zaväzujete sa, že nebudete:</p>
<ul>
<li>používať Oncofiles na iné účely než osobnú správu zdravotných dokumentov</li>
<li>pokúšať sa o prístup k údajom iných používateľov</li>
<li>spätne analyzovať API ani ho zneužívať</li>
<li>používať službu na komerčné spracúvanie zdravotných údajov bez oprávnenia</li>
</ul>

<h2 id="availability">6. Dostupnosť</h2>
<p>Snažíme sa o vysokú dostupnosť, ale nezaručujeme nepretržitú prevádzku. Oncofiles sa
poskytuje „tak, ako je“, bez akýchkoľvek záruk. Nezodpovedáme za stratu údajov nad rámec toho,
čo je uložené vo vašom Google Drive. Oncofiles nikdy natrvalo nemaže súbory: len ich premenúva
a presúva v rámci priečinka, ktorý ste vybrali.</p>

<h2 id="ending">7. Ukončenie používania</h2>
<p>Prístup Oncofiles k vášmu Google Drive môžete kedykoľvek zastaviť vo svojom
<a href="https://myaccount.google.com/permissions">účte Google</a> alebo ako opatrovateľ
pacienta tlačidlom „Odpojiť Google Drive“ v dashboarde, kde si môžete stiahnuť aj export údajov
uložených o pacientovi. Ak chcete tieto údaje vymazať (alebo aby sme ich exportovali my),
napíšte nám e-mail; takéto žiadosti vybavujeme ručne, do 30 dní. Vaše súbory v Google Drive
zostanú tam, kde sú.</p>

<h2 id="changes">8. Zmeny</h2>
<p>Tieto podmienky môžeme aktualizovať. Dátum v hornej časti stránky ukazuje najnovšiu verziu.
Keď zmeníme to, s čím súhlasíte pri vytváraní pacienta, zmení sa verzia súhlasu.</p>

<h2 id="contact">9. Kontakt</h2>
<p><a href="mailto:__CONTACT__">__CONTACT__</a></p>
<p>Instarea s.r.o. · Bratislava, Slovensko</p>
"""
