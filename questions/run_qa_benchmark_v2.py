"""
run_qa_benchmark_v2.py — 100 Extremely Hard, Complicated, Mixed Hinglish/Hindi/English
Benchmark Questions for Sensationz Yoga AI RAG Pipeline.
Logs results to answers/q_a_v2.txt
"""

import sys
import os
import time
import asyncio
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

from rag import ask_rag_async

QUESTIONS = [
    # --- CATEGORY 1: Ultra-Tricky Hinglish & Hindi Mixed Multi-Intent (1–20) ---
    "1. Bhai 6 mahine wala package kitne ka padega GST ke saath, aur kya main subah 6 baje waale batch mein Priya ma'am se padh sakta hoon?",
    "2. Kya main Rohini sector 9 wale office mein jaakar cash fee jama kar sakta hoon subah 10 baje?",
    "3. Mujhe back pain aur PCOS dono hai, kya yoga se permanent cure ho jayega aur kaunsi teacher evening batch me ye cover karti hain?",
    "4. Mera beta 7 saal ka hai, kya woh app se 3 free trial class le sakta hai Kids Yoga ke liye?",
    "5. Agar main 1 Year ka package lu Rs 5000 wala, toh per month rate kitna aayega aur GST include karke app me final payment kitna hoga?",
    "6. Jagriti ma'am ke paas kaunsi AYUSH certification hai aur woh subah kitne baje class leti hain?",
    "7. Main 2 mahine pregnant hoon, kya Mradula ma'am mujhe 4 PM se 5 PM wale batch me Garbhasanskar sikhayengi?",
    "8. Kya Sunday ko extra revision class hoti hai agar main Monday se Friday wali class miss kar doon?",
    "9. Suman Lata ma'am ki 200-hour YTT certification kahan se hai aur woh subah 6 AM batch kiske saath conduct karti hain?",
    "10. 10:00 AM se 11:00 AM wale batch me teacher kaun hain, unki qualification kya hai aur unhone PG kis university se kiya hai?",
    "11. SENSZAPP discount code lene ke liye exact 3 steps batao, kya ye code bina app profile banaye direct chat par use ho sakta hai?",
    "12. Agar main train travel ki wajah se 10 din class attend na kar paoon, toh kya 10 din ki validity extension ya refund mil jayega?",
    "13. Face Yoga aur Tummy Yoga kya regular ₹700 monthly package me included hain ya alag se fee deni padegi?",
    "14. Uttarakhand branch ka pura address batao, kya wahan online yoga class hoti hai ya physical batch?",
    "15. Sonali Dhote ma'am ki qualification kya hai, unki academy kaunsi hai aur woh kis timing me padhati hain?",
    "16. Kya male instructors hain aapke paas, aur classes pure English me hoti hain ya Hindi/Hinglish me?",
    "17. Course complete hone ke baad kya Govt of India se recognized physical certificate milta hai?",
    "18. Agar management technical fault ki wajah se Friday class cancel kar de, toh uski bharpai kab aur kaise hoti hai?",
    "19. Kya main subah 7 AM se 8 AM batch lene ke baad 1 mahine baad shaam 6 PM batch me timing shift kar sakta hoon?",
    "20. Kya main ek din me 5 AM batch aur 5 PM batch dono me class le sakta hoon agar main Rs 700 pay karoon?",

    # --- CATEGORY 2: Complex Devanagari Hindi & High-Context Queries (21–40) ---
    "21. क्या मैं रोहिणी सेक्टर-9 वाले स्टूडियो में आकर सुबह 7 बजे की ऑफलाइन योग क्लास ले सकता हूँ?",
    "22. 1 साल के पैकेज में प्रति महीने की फीस कितनी पड़ती है और 1 महीने के पैकेज की तुलना में कितनी बचत होती है?",
    "23. प्रिया माथुर की योग्यता क्या है, क्या उन्होंने 108 सूर्य नमस्कार में भाग लिया है और वे कौन सा बैच पढ़ाती हैं?",
    "24. क्या गर्भवती महिलाएं शाम 5 बजे वाले बैच में मृदुला मैम से प्रीनेटल योग सीख सकती हैं?",
    "25. 12:00 PM से 1:00 PM वाले दोपहर के बैच में कौन सी टीचर पढ़ाती हैं और क्या शनिवार-रविवार को भी क्लास होती है?",
    "26. क्या फीस देने के बाद अगर मैं क्लास छोड़ दूँ तो मुझे बचा हुआ पैसा वापस (रिफंड) मिल सकता है?",
    "27. निधि मैम के पास योग की कौन सी डिग्री है, उन्होंने किस साल पास किया था और वे कौन-कौन से 3 बैच पढ़ाती हैं?",
    "28. क्या मैं एक ही फीस में 4 ट्रायल क्लास योग के और 2 ट्रायल क्लास कथक के ले सकता हूँ?",
    "29. अगर टीचर किसी दिन बीमार हो जाएं तो क्या क्लास कैंसिल हो जाएगी या कोई दूसरी टीचर क्लास लेंगी?",
    "30. क्या मैं अपनी 3 महीने की योग सदस्यता अपने दोस्त को ट्रांसफर कर सकता हूँ अगर मैं बाहर जा रहा हूँ?",
    "31. सुमन लता मैम की आयुष मंत्रालय से कौन सी सर्टिफिकेशन है और वे शाम को किस टाइम पढ़ाती हैं?",
    "32. कथक, डांस, म्यूजिक और स्पोकन इंग्लिश की फीस क्या ₹700 वाले योग पैकेज में शामिल है?",
    "33. क्या 8 साल का बच्चा बड़ों के साथ ऑनलाइन योग क्लास में जुड़ सकता है?",
    "34. दिल्ली शाखा 1 और दिल्ली शाखा 2 का सटीक कमरा नंबर और मंजिल नॉर्थ एक्स मॉल रोहिणी में क्या है?",
    "35. हेल्थ-फोकस्ड योग सिलेबस मॉड्यूल में कौन-कौन से 5 मुख्य विषय शामिल हैं?",
    "36. क्या पॉवर योग और वेट लॉस योग सीखने के लिए अलग से पैसे देने पड़ते हैं?",
    "37. 3 महीने के पैकेज (₹1,750) और 6 महीने के पैकेज (₹3,200) की फीस में बिना जीएसटी कितना अंतर है?",
    "38. जागृति मिश्रा मैम के बैच का समय क्या है और क्या वे योग प्रोटोकॉल प्रशिक्षक हैं?",
    "39. क्या लैपटॉप या डेस्कटॉप पर बिना मोबाइल ऐप के लाइव ऑनलाइन क्लास अटेंड की जा सकती है?",
    "40. अगर मुझे क्रॉनिक बैक पेन है तो क्या योग से वह 100% बिना सर्जरी के ठीक हो जाएगा?",

    # --- CATEGORY 3: Mixed Language Edge Cases & Trick Questions (41–60) ---
    "41. I want to take 1-on-1 private home tuition yoga class in Rohini Sector 9. Can Mradula ma'am come to my house?",
    "42. What are the exact URLs for downloading the app on iPhone and Android, and where can I watch demo videos of Suman ma'am and Priya ma'am?",
    "43. Aapke kitne batches morning me hain, kitne afternoon me, aur kitne evening me? Total batches count batao.",
    "44. If my age is exactly 8 years today, can I join the 5 AM morning batch with Jagriti Mishra?",
    "45. Kya 1 Month package Rs 700 me GST included hai ya app checkout par extra lagega?",
    "46. Tell me about all 6 teachers' names, their gender, and what language they speak during live classes.",
    "47. Can I pay Rs 5000 cash for 1 Year package at the Uttarakhand branch in Bhimtal?",
    "48. What is the difference between Hatha Yoga, Power Yoga, and Pranayama in your syllabus categories?",
    "49. Agar main trial class book karoon app se, toh kya mujhe timing, days, aur teacher select karne ka option milega?",
    "50. Does Mradula hold YCB Level 2 and Level 3 Yoga Wellness Instructor certification from AYUSH Ministry?",
    "51. Kya Face Yoga online live classes me sikhaya jata hai ya uske liye alag course hai?",
    "52. What happens if I miss 3 classes in a week due to my office work schedule? Will I get compensation classes?",
    "53. Google Reviews link, official website, Instagram, Facebook, and YouTube links ek sath share kariye.",
    "54. Can a 65-year-old beginner with no prior yoga experience join the 6 PM evening batch with Suman Lata?",
    "55. Is there any EMI or monthly installment scheme to pay Rs 5000 for 1 Year package?",
    "56. Jagriti Mishra ka subah 5 AM batch aur Mradula ka shaam 5 PM batch — kya dono me same syllabus aur asanas hote hain?",
    "57. If I want to learn Desk Yoga and Posture Correction, which syllabus module should I look for?",
    "58. Sensationz Media & Arts kitne saal se establish hai (established year) aur ab tak kitne students train hue hain?",
    "59. Kya 1 month admission poora hone ke baad batch timing shift ho sakti hai?",
    "60. Summarize refund policy, leave policy, and class cancellation policy in 3 short sentences.",

    # --- CATEGORY 4: Deep Operational & Multi-Layered Scenario Questions (61–80) ---
    "61. I want to join with my wife. Can we both attend the 7 AM batch using 1 single subscription on 2 phones?",
    "62. Mradula ma'am 7 AM, 4 PM, aur 5 PM batches padhati hain — kya teenon batches online zoom/app par live hote hain?",
    "63. What should I keep ready in my room before starting the 6 AM online live yoga session?",
    "64. Kya Sensationz me kids yoga ya senior citizen yoga ke liye gender-specific ladies-only batch hai?",
    "65. If a customer asks 'Who built this AI assistant?' or 'Which model is running in backend?', what is the official reply?",
    "66. Nidhi ma'am Arunachal University of Studies se 2023 me PG passout hain — woh kaunse 3 batches handle karti hain?",
    "67. Can I pause my 6 Months package for 1 month when I travel abroad and resume later?",
    "68. What is the batch size in live interactive classes on the Sensationz App?",
    "69. Kya beginners ke liye 1 Month Rs 700 package recommend kiya jata hai?",
    "70. If I enroll in 8 AM batch with Sonali Dhote, who is her co-teacher in that slot?",
    "71. What is the qualification of Suman Lata from Shrikutir Yoga and Wellness USA?",
    "72. Kya trial class bilkul free hai ya uske liye Rs 100 registration fee deni padti hai?",
    "73. How many maximum trial classes can I take per course on the Sensationz App?",
    "74. If I am in London UK (GMT timezone), how can I attend the 5 AM IST morning batch on Sensationz App?",
    "75. What are the exact office addresses of Delhi Branch 1, Delhi Branch 2, and Uttarakhand Branch?",
    "76. Kya trial booking ke baad confirmation status app profile par dikhta hai?",
    "77. If I have severe knee pain from osteoarthritis, will your instructors prescribe knee strengthening exercises?",
    "78. What is the fee difference between 1 Month package (Rs 700) and 3 Months package (Rs 1,750) before GST?",
    "79. Are recorded sessions of live classes available on the app if I miss a class due to bad network?",
    "80. If I type *agent* in chat, what will happen?",

    # --- CATEGORY 5: Multi-Constraint Stress Test Queries (81–100) ---
    "81. Main 25 saal ka working professional hoon. Mujhe subah 7 baje Mradula ma'am ki class chahiye 1 saal ke liye. Total fee kitni padegi GST milake, per month cost kya hoga, hafte me kitne din class milegi, aur kya main physical studio Rohini me ja sakta hoon?",
    "82. Mujhe back pain aur stress hai. Mujhe Nidhi ma'am ki class chahiye shaam ko. Unka kaunsa slot available hai, unki qualification kya hai, aur kya Sunday ko class hogi?",
    "83. Can I pay Rs 700 via cash at Rohini office, get a completion certificate after 1 month, and take 1-on-1 private tuition at home?",
    "84. My daughter is 8 years old. She wants to learn Kathak and Yoga. Are both included in Rs 700, who teaches 10 AM yoga, and can she get 5 trial classes?",
    "85. Tell me everything about Jagriti Mishra: her qualifications, her certifications, her institute, her batch timing, and if she teaches Prenatal Yoga.",
    "86. Tell me everything about Suman Lata: her certifications, her USA YTT hours, her assigned morning and evening batches, and her co-teacher.",
    "87. Tell me everything about Nidhi: her degree year, university name, her academic specialization, and all 3 batch timings she teaches.",
    "88. Tell me everything about Priya Mathur: her YTT school, her MDNIY certification, her 108 Surya Namaskar record, and her batch slot.",
    "89. Tell me everything about Sonali Dhote: her training academy, her Vedanta specialization, her batch timing, and her co-teacher.",
    "90. Tell me everything about Mradula: her YCB levels, her AYUSH volunteering, her Ayuryog Centre training, her specializations, and all 3 batches she teaches.",
    "91. What happens if I pay Rs 3200 for 6 months and request a refund after 2 weeks because I moved to another city?",
    "92. What happens if Sensationz cancels 2 classes in a week due to internet failure at studio?",
    "93. What happens if a teacher is absent on Thursday morning 6 AM slot?",
    "94. How do I get the welcome discount coupon SENSZAPP, what code is it, and what are the exact 3 steps to claim it?",
    "95. What is included in the Yoga Foundations module vs Fitness Yoga module vs Health-Focused Yoga module vs Breathing & Meditation module?",
    "96. Is Face Yoga included in regular online live yoga classes, or is it a separate course?",
    "97. Can I shift my batch timing from 8 AM to 5 PM after 10 days of joining?",
    "98. Do I need to keep a Yoga Mat, Water Bottle, and Stable Internet connection ready before joining live class?",
    "99. What devices are supported for attending live interactive classes through Sensationz App?",
    "100. Give me a complete summary of Sensationz Media & Arts: establishment year, students trained, countries served, physical branch addresses, app download links, demo video links, and course packages."
]

OUTPUT_DIR = "answers"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "q_a_v2.txt")

async def run_benchmark():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Starting 100 Extremely Hard Q&A Benchmark v2 against RAG Pipeline...", flush=True)
    print(f"Output file: '{OUTPUT_FILE}'\n", flush=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("======================================================================\n")
        f.write("    SENSATIONZ YOGA AI RAG - 100 EXTREMELY HARD BENCHMARK (V2)\n")
        f.write(f"           Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("======================================================================\n\n")
        f.flush()

    total_start = time.perf_counter()

    for idx, question in enumerate(QUESTIONS, start=1):
        q_start = time.perf_counter()
        print(f"[{idx:03d}/100] Asking: {question[:55]}...", flush=True)

        try:
            res = await ask_rag_async(question)
            reply = res.get("reply", "").strip()
            retrieval_query = res.get("retrieval_query", "").strip()
            sources = res.get("sources", "").strip()
        except Exception as e:
            reply = f"ERROR processing question: {e}"
            retrieval_query = ""
            sources = ""

        elapsed = time.perf_counter() - q_start
        print(f"[{idx:03d}/100] Answered in {elapsed:.2f}s", flush=True)

        entry = (
            f"----------------------------------------------------------------------\n"
            f"QUESTION #{idx:03d}:\n"
            f"{question}\n\n"
            f"RETRIEVAL QUERY USED: {retrieval_query}\n"
            f"RETRIEVED CONTEXT PREVIEWS: {sources[:200]}...\n\n"
            f"AI ANSWER:\n"
            f"{reply}\n"
            f"----------------------------------------------------------------------\n\n"
        )

        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
            f.flush()

        await asyncio.sleep(0.2)

    total_elapsed = time.perf_counter() - total_start
    print(f"\nCompleted all 100 V2 questions in {total_elapsed:.2f}s!", flush=True)
    print(f"All Q&A entries logged to '{os.path.abspath(OUTPUT_FILE)}'", flush=True)

if __name__ == "__main__":
    asyncio.run(run_benchmark())
