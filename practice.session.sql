-- SELECT ed.employee_id, age, occupation
-- FROM employee_demographics AS ed
-- JOIN employee_salary AS es
--  ON ed.employee_id = es.employee_id


--Outer Joints 


-- SELECT ed.employee_id, age, occupation
-- FROM employee_demographics AS ed
-- RIGHT JOIN employee_salary AS es
--  ON ed.employee_id = es.employee_id



--Self Joint

-- SELECT *
-- FROM employee_salary AS em1
-- JOIN employee_salary AS em2
--  ON em1.employee_id + 1 = em2.employee_id;


-- Multi Table joins ( when they have no common thing)
-- SELECT *
-- FROM employee_salary AS em1
-- JOIN employee_salary AS em2
--  ON em1.employee_id = em2.employee_id

-- JOIN parks_departments AS pd 
--  ON em2.dept_id = pd.department_id





--Case Statement

-- SELECT first_name, last_name,
--  CASE 
--     WHEN age <= 30 THEN 'YOUNG'
--     WHEN age > 30 THEN 'OLD'
--  END
-- FROM employee_demographics;


-- SELECT first_name, last_name,employee_salary,
--  CASE 
--     WHEN employee_salary < 5000 THEN employee_salary * 1.05 
--     WHEN employee_salary > 5000 THEN employee_salary * 1.02
--  END AS New_salary
-- FROM employee_demographics;



--Sub Queries

SELECT *

FROM employee_demographics
WHERE employee_id IN
      (SELECT employee_id
         FROM employee_salary
         WHERE dept_id =1 )